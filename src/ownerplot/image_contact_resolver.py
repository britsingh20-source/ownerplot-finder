from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from PIL import Image

from .domain import Listing
from .processing import normalize_phone

PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?([6-9](?:[\s.-]?\d){9})(?!\d)")
IMG_RE = re.compile(r"<(?:img|source)\b[^>]*(?:src|data-src|data-original|srcset)=[\"']([^\"']+)[\"'][^>]*>", re.I)
PORTALS = {"magicbricks.com", "99acres.com"}
SKIP_IMAGE_TERMS = ("logo", "icon", "sprite", "avatar", "profile", "placeholder", "loader", "banner", "ads", "advert")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _dhash(image: Image.Image, size: int = 8) -> int:
    gray = image.convert("L").resize((size + 1, size))
    pixels = list(gray.getdata())
    value = 0
    for row in range(size):
        offset = row * (size + 1)
        for col in range(size):
            value = (value << 1) | int(pixels[offset + col] > pixels[offset + col + 1])
    return value


def _distance(first: int, second: int) -> int:
    return (first ^ second).bit_count()


def _image_urls(html: str, base_url: str, limit: int = 12) -> list[str]:
    output=[]; seen=set()
    for raw in IMG_RE.findall(html or ""):
        raw=raw.split(",")[0].strip().split()[0]
        url=urljoin(base_url,raw)
        lower=url.lower()
        if not url.startswith("https://") or any(term in lower for term in SKIP_IMAGE_TERMS):
            continue
        if url not in seen:
            seen.add(url);output.append(url)
        if len(output)>=limit:break
    return output


def _tokens(item: Listing) -> list[str]:
    text=f"{item.title} {item.description}"
    terms=[]
    for pattern in (r"\b\d{2,3}\s*[xX×]\s*\d{2,3}\b", r"\b[A-Za-z][A-Za-z ]{3,30}(?:Garden|Avenue|Nagar|Phase|Colony)\b"):
        terms.extend(re.findall(pattern,text,re.I))
    return terms[:3]


@dataclass(slots=True)
class ImageContactCandidate:
    url: str
    phone: str
    distance: int
    seed_image: str
    donor_image: str


class PropertyImageContactResolver:
    """Find public phone cross-posts by matching property photos with perceptual dHash.

    This performs no face recognition. It compares whole property images only.
    """

    def __init__(self, google_key: str, cse_id: str, tavily_key: str, allowed_domains: set[str], client: httpx.AsyncClient | None = None) -> None:
        self.google_key=google_key;self.cse_id=cse_id;self.tavily_key=tavily_key
        self.allowed_domains={d.lower().removeprefix("www.") for d in allowed_domains}
        self.client=client or httpx.AsyncClient(timeout=15,follow_redirects=True,headers={"User-Agent":"OwnerPlotFinder/0.7 (+property-image-correlation)"})
        self.max_seeds=int(os.getenv("PROPERTY_IMAGE_MAX_SEEDS","4"))
        self.max_pages=int(os.getenv("PROPERTY_IMAGE_MAX_DONOR_PAGES","12"))
        self.max_distance=int(os.getenv("PROPERTY_IMAGE_DHASH_DISTANCE","6"))

    @classmethod
    def from_environment(cls):
        google=os.getenv("GOOGLE_CSE_API_KEY","").strip();cse=os.getenv("GOOGLE_CSE_ID","").strip();tavily=os.getenv("TAVILY_API_KEY","").strip()
        if not (google and cse) and not tavily:return None
        registry=Path(__file__).resolve().parents[2]/"config"/"allowed-public-domains.txt";domains=set()
        try:domains={line.strip() for line in registry.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")}
        except OSError:pass
        domains|={"realestateindia.com","housing.com","nobroker.in","quikr.com","olx.in","facebook.com","instagram.com","youtube.com"}
        return cls(google,cse,tavily,domains)

    def _allowed(self,url:str)->bool:
        host=_host(url);return urlparse(url).scheme=="https" and bool(host) and any(host==d or host.endswith(f".{d}") for d in self.allowed_domains|PORTALS)

    async def _robots(self,url:str)->bool:
        parsed=urlparse(url)
        try:
            r=await self.client.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
            if r.status_code>=400:return False
            p=RobotFileParser();p.parse(r.text.splitlines());return p.can_fetch("OwnerPlotFinder",url)
        except httpx.HTTPError:return False

    async def _html(self,url:str)->str:
        if not self._allowed(url) or not await self._robots(url):return ""
        try:
            r=await self.client.get(url)
            return r.text if r.status_code<400 and "text/html" in r.headers.get("content-type","").lower() else ""
        except httpx.HTTPError:return ""

    async def _hash(self,url:str)->int|None:
        if not url.startswith("https://"):return None
        try:
            r=await self.client.get(url)
            if r.status_code>=400 or len(r.content)>5_000_000 or not r.headers.get("content-type","").lower().startswith("image/"):return None
            with Image.open(BytesIO(r.content)) as image:
                if image.width<200 or image.height<150:return None
                return _dhash(image)
        except (httpx.HTTPError,OSError,ValueError):return None

    async def _seed_hashes(self,item:Listing)->list[tuple[str,int]]:
        html=await self._html(item.url);urls=_image_urls(html,item.url,8)
        values=await asyncio.gather(*(self._hash(url) for url in urls))
        return [(url,value) for url,value in zip(urls,values) if value is not None][:4]

    def _query(self,item:Listing)->str:
        area=f'"{round(item.area_sqft)} sqft"' if item.area_sqft else ""
        owner=(item.seller_claim or "").strip(); owner=f'"{owner}"' if owner.lower() not in {"","owner","individual"} else ""
        extra=" ".join(f'"{term}"' for term in _tokens(item))
        return f'"{item.locality}" {area} {owner} {extra} plot property phone whatsapp'.strip()

    async def _google(self,q:str)->list[dict]:
        if not self.google_key or not self.cse_id:return []
        try:
            r=await self.client.get("https://www.googleapis.com/customsearch/v1",params={"key":self.google_key,"cx":self.cse_id,"q":q,"num":10});r.raise_for_status()
            return [{"url":x.get("link",""),"text":f"{x.get('title','')} {x.get('snippet','')}"} for x in r.json().get("items",[])]
        except (httpx.HTTPError,ValueError,KeyError):return []

    async def _tavily(self,q:str)->list[dict]:
        if not self.tavily_key:return []
        donors=sorted(self.allowed_domains-PORTALS)
        try:
            r=await self.client.post("https://api.tavily.com/search",headers={"Authorization":f"Bearer {self.tavily_key}","Content-Type":"application/json"},json={"query":q,"topic":"general","search_depth":"basic","max_results":10,"include_answer":False,"include_raw_content":"text","include_images":False,"include_domains":donors});r.raise_for_status()
            return [{"url":x.get("url",""),"text":f"{x.get('title','')} {x.get('content','')} {x.get('raw_content','') or ''}"} for x in r.json().get("results",[])]
        except (httpx.HTTPError,ValueError,KeyError):return []

    async def _resolve_one(self,item:Listing)->ImageContactCandidate|None:
        seeds=await self._seed_hashes(item)
        if not seeds:
            item.evidence.append("Image hunt unavailable: no fetchable public property images on seed page")
            return None
        google,tavily=await asyncio.gather(self._google(self._query(item)),self._tavily(self._query(item)))
        seen=set();pages=[]
        for result in [*google,*tavily]:
            url=result.get("url","")
            if not url or url in seen or _host(url) in PORTALS or not self._allowed(url):continue
            seen.add(url);pages.append(result)
            if len(pages)>=self.max_pages:break
        best=None;compared=phone_pages=0
        for result in pages:
            url=result["url"];html=await self._html(url);text=f"{result.get('text','')} {re.sub(r'<[^>]+>',' ',html)[:50000]}"
            phones={normalize_phone(m.group(0)) for m in PHONE_RE.finditer(text)};phones.discard(None)
            if not phones:continue
            phone_pages+=1;images=_image_urls(html,url,10);hashes=await asyncio.gather(*(self._hash(image) for image in images))
            for donor_url,donor_hash in zip(images,hashes):
                if donor_hash is None:continue
                for seed_url,seed_hash in seeds:
                    compared+=1;distance=_distance(seed_hash,donor_hash)
                    if distance<=self.max_distance:
                        for phone in phones:
                            candidate=ImageContactCandidate(url,phone,distance,seed_url,donor_url)
                            if best is None or candidate.distance<best.distance:best=candidate
        item.evidence.append(f"Image hunt checked {len(pages)} donor pages; {phone_pages} had phones; compared {compared} image pairs")
        return best

    async def enrich(self,listings:list[Listing])->list[Listing]:
        targets=[item for item in listings if not item.phone][:self.max_seeds]
        results=await asyncio.gather(*(self._resolve_one(item) for item in targets),return_exceptions=True)
        for item,result in zip(targets,results):
            if isinstance(result,BaseException) or result is None:continue
            item.phone=result.phone;item.phone_public=True;item.contact_verification="public_image_matched_owner_contact"
            item.evidence.append(f"same property image: dHash distance {result.distance}/{64}")
            item.evidence.append(f"Public phone found on image-matched cross-post: {result.url}")
        return listings
