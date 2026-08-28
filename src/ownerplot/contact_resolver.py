from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .domain import Listing, SellerType
from .processing import normalize_phone, classify_seller

PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?([6-9](?:[\s.-]?\d){9})(?!\d)")
PARTIAL_PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?([6-9]\d{1,4})[\s.-]?[xX*•]{4,}")
OWNER_NAME_RE = re.compile(r"\bowner\s*[:\-]\s*([A-Za-z][A-Za-z .]{1,60})", re.I)
DIM_RE = re.compile(r"\b(\d{2,3}(?:\.\d+)?)\s*[xX×]\s*(\d{2,3}(?:\.\d+)?)\b")
PORTAL_HOSTS = {"magicbricks.com", "99acres.com"}
GENERIC = {"plot","land","property","sale","owner","residential","coimbatore","kalapatti","sqft","square","feet","road","near","contact","price","facing","resale","freehold"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_portal(item: Listing) -> bool:
    host = _host(item.url)
    return host in PORTAL_HOSTS or any(host.endswith(f".{domain}") for domain in PORTAL_HOSTS)


def _owner_name(item: Listing) -> str | None:
    for text in (item.seller_claim or "", item.title, item.description):
        match = OWNER_NAME_RE.search(text or "")
        if match:
            name = " ".join(match.group(1).split()).strip(" .,-")
            if name.lower() not in {"owner", "contact owner", "individual"}:
                return name
    claim = (item.seller_claim or "").strip()
    return claim if claim and claim.lower() not in {"owner","contact owner","posted by owner","individual"} else None


def _dimensions(text: str) -> set[tuple[int,int]]:
    return {tuple(sorted((round(float(a)),round(float(b))))) for a,b in DIM_RE.findall(text or "")}


def _visible_phone_prefix(text: str) -> str | None:
    match = PARTIAL_PHONE_RE.search(text or "")
    return match.group(1) if match and len(match.group(1)) >= 2 else None


def _tokens(text: str) -> set[str]:
    values=set(re.findall(r"[a-z0-9]+",(text or "").lower()))
    return {v for v in values if len(v)>2 and v not in GENERIC}


def _page_text(item: Listing) -> str:
    return f"{item.title} {item.description}"


def _score(target: Listing,candidate_text: str,candidate_url: str)->tuple[int,list[str]]:
    text=candidate_text.lower(); evidence=[]; score=0
    locality=target.locality.lower().strip()
    if locality and locality in text: score+=20; evidence.append("same locality")
    owner=_owner_name(target)
    if owner and owner.lower() in text: score+=25; evidence.append("same owner name")
    if target.area_sqft:
        area=round(target.area_sqft); variants={str(area),f"{area:,}",f"{target.area_sqft:g}"}
        if any(re.search(rf"\b{re.escape(v)}\s*(?:sq\.?\s*ft|sqft|square\s*feet)\b",text,re.I) for v in variants): score+=30; evidence.append("same plot area")
    if _dimensions(_page_text(target)) & _dimensions(candidate_text): score+=20; evidence.append("same dimensions")
    prefix=_visible_phone_prefix(_page_text(target))
    if prefix and re.search(rf"(?:\+?91[\s.-]?)?{re.escape(prefix)}\d+",candidate_text): score+=15; evidence.append("same visible phone prefix")
    if target.price:
        lakhs=target.price/100_000; crores=target.price/10_000_000
        if any(x in text for x in (f"{lakhs:g} lakh",f"{lakhs:g} lac",f"{crores:g} cr",f"{crores:g} crore")): score+=10; evidence.append("same asking price")
    overlap=_tokens(_page_text(target))&_tokens(candidate_text)
    if len(overlap)>=5: score+=15; evidence.append("strong description/project overlap")
    elif len(overlap)>=3: score+=8; evidence.append("description/project overlap")
    if _host(candidate_url) in PORTAL_HOSTS: score-=15
    return max(0,min(100,score)),evidence


def _queries(target: Listing)->list[str]:
    locality=target.locality; owner=_owner_name(target); area=round(target.area_sqft) if target.area_sqft else None
    dims=sorted(_dimensions(_page_text(target))); dim=f"{dims[0][0]} X {dims[0][1]}" if dims else None
    prefix=_visible_phone_prefix(_page_text(target)); distinctive=sorted(_tokens(_page_text(target))); token_phrase=" ".join(f'"{x}"' for x in distinctive[:3])
    q=[]
    if area: q.append(f'"{locality}" "{area} sqft" (phone OR contact OR whatsapp) plot')
    if dim: q.append(f'"{locality}" "{dim}" (phone OR contact OR whatsapp)')
    if owner and area: q.append(f'"{owner}" "{locality}" "{area}" property contact')
    if owner and dim: q.append(f'"{owner}" "{dim}" Coimbatore')
    if prefix and area: q.append(f'"{locality}" "{area}" "{prefix}" phone')
    if prefix and owner: q.append(f'"{owner}" "{prefix}" Coimbatore')
    if token_phrase: q.append(f'"{locality}" {token_phrase} (phone OR whatsapp OR contact)')
    if area:
        for site in ("realestateindia.com","housing.com","nobroker.in","facebook.com","instagram.com","youtube.com","olx.in","quikr.com"):
            q.append(f'site:{site} "{locality}" "{area}" property')
    return list(dict.fromkeys(q))


@dataclass(slots=True)
class ContactCandidate:
    url:str; phone:str; score:int; evidence:list[str]


class PublicOwnerContactResolver:
    def __init__(self,api_key:str,cse_id:str,allowed_domains:set[str],tavily_api_key:str="",client:httpx.AsyncClient|None=None)->None:
        self.api_key=api_key; self.cse_id=cse_id; self.tavily_api_key=tavily_api_key
        self.allowed_domains={d.lower().removeprefix("www.") for d in allowed_domains}
        self.client=client or httpx.AsyncClient(timeout=20,follow_redirects=True,headers={"User-Agent":"OwnerPlotFinder/0.5 (+public-contact-correlation)"})
        self.max_listings=int(os.getenv("PUBLIC_CONTACT_RESOLVE_MAX_LISTINGS","8")); self.max_queries=int(os.getenv("PUBLIC_CONTACT_RESOLVE_QUERIES","8")); self.min_score=int(os.getenv("PUBLIC_CONTACT_MIN_MATCH_SCORE","75"))

    @classmethod
    def from_environment(cls):
        key=os.getenv("GOOGLE_CSE_API_KEY","").strip(); cse=os.getenv("GOOGLE_CSE_ID","").strip(); tavily=os.getenv("TAVILY_API_KEY","").strip()
        if not (key and cse) and not tavily: return None
        registry=Path(__file__).resolve().parents[2]/"config"/"allowed-public-domains.txt"; domains=set()
        try: domains={line.strip() for line in registry.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")}
        except OSError: pass
        domains|={"realestateindia.com","housing.com","nobroker.in","quikr.com","olx.in","facebook.com","instagram.com","youtube.com"}
        return cls(key,cse,domains,tavily)

    def _allowed(self,url:str)->bool:
        host=_host(url); return urlparse(url).scheme=="https" and bool(host) and any(host==d or host.endswith(f".{d}") for d in self.allowed_domains)

    async def _robots_allowed(self,url:str)->bool:
        parsed=urlparse(url)
        try:
            response=await self.client.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
            if response.status_code>=400:return False
            parser=RobotFileParser(); parser.parse(response.text.splitlines()); return parser.can_fetch("OwnerPlotFinder",url)
        except httpx.HTTPError:return False

    async def _google_search(self,query:str)->list[dict]:
        if not self.api_key or not self.cse_id:return []
        try:
            response=await self.client.get("https://www.googleapis.com/customsearch/v1",params={"key":self.api_key,"cx":self.cse_id,"q":query,"num":10}); response.raise_for_status()
            return [{"link":i.get("link",""),"title":i.get("title",""),"snippet":i.get("snippet","")} for i in response.json().get("items",[])]
        except (httpx.HTTPError,ValueError,KeyError):return []

    async def _tavily_search(self,query:str)->list[dict]:
        if not self.tavily_api_key:return []
        donor_domains=sorted(d for d in self.allowed_domains if d not in PORTAL_HOSTS)
        try:
            response=await self.client.post("https://api.tavily.com/search",headers={"Authorization":f"Bearer {self.tavily_api_key}","Content-Type":"application/json"},json={"query":query,"topic":"general","search_depth":"basic","max_results":10,"include_answer":False,"include_raw_content":"text","include_images":False,"include_domains":donor_domains}); response.raise_for_status()
            return [{"link":i.get("url",""),"title":i.get("title",""),"snippet":i.get("content","") or "","raw":i.get("raw_content","") or ""} for i in response.json().get("results",[])]
        except (httpx.HTTPError,ValueError,KeyError):return []

    async def _search(self,query:str)->list[dict]:
        google,tavily=await asyncio.gather(self._google_search(query),self._tavily_search(query)); seen=set(); out=[]
        for item in [*google,*tavily]:
            url=item.get("link","")
            if url and url not in seen:seen.add(url);out.append(item)
        return out

    async def _candidate_text(self,item:dict)->tuple[str,str]:
        url=item.get("link",""); snippet=" ".join(str(item.get(k,"")) for k in ("title","snippet","raw"))
        if not url or not self._allowed(url) or not await self._robots_allowed(url):return url,snippet
        try:
            response=await self.client.get(url)
            if response.status_code>=400:return url,snippet
            if not any(t in response.headers.get("content-type","").lower() for t in ("text/html","text/plain")):return url,snippet
            body=re.sub(r"<script\b[^>]*>.*?</script>"," ",response.text,flags=re.I|re.S); body=re.sub(r"<style\b[^>]*>.*?</style>"," ",body,flags=re.I|re.S); body=re.sub(r"<[^>]+>"," ",body); body=re.sub(r"\s+"," ",body)
            return url,f"{snippet} {body[:80000]}"
        except httpx.HTTPError:return url,snippet

    async def _resolve_one(self,target:Listing)->ContactCandidate|None:
        seen=set();best=None;checked=phone_pages=best_score=0
        for query in _queries(target)[:self.max_queries]:
            for item in await self._search(query):
                url=item.get("link","")
                if not url or url in seen or not self._allowed(url) or url.rstrip("/")==target.url.rstrip("/"):continue
                seen.add(url);checked+=1;candidate_url,text=await self._candidate_text(item)
                phones={normalize_phone(m.group(0)) for m in PHONE_RE.finditer(text)};phones.discard(None)
                if not phones:continue
                phone_pages+=1;score,evidence=_score(target,text,candidate_url);best_score=max(best_score,score)
                if score<self.min_score or not any(k in evidence for k in ("same plot area","same dimensions","strong description/project overlap")):continue
                for phone in phones:
                    candidate=ContactCandidate(candidate_url,phone,score,evidence)
                    if best is None or candidate.score>best.score:best=candidate
        target.evidence.append(f"Contact hunt checked {checked} public candidates; {phone_pages} exposed full phones; best match {best_score}/100")
        return best

    async def enrich(self,listings:list[Listing])->list[Listing]:
        targets=[i for i in listings if _is_portal(i) and not i.phone and i.seller_type not in {SellerType.BROKER,SellerType.BUILDER} and (i.seller_type in {SellerType.PROBABLE_OWNER,SellerType.VERIFIED_OWNER} or re.search(r"\b(owner|contact owner|posted by owner|individual)\b",f"{i.seller_claim or ''} {i.title} {i.description}",re.I))][:self.max_listings]
        resolved=await asyncio.gather(*(self._resolve_one(i) for i in targets),return_exceptions=True)
        for target,result in zip(targets,resolved):
            if isinstance(result,BaseException) or result is None:continue
            target.phone=result.phone;target.phone_public=True;target.matching_contact_sources=max(2,target.matching_contact_sources);target.contact_verification="public_cross_post_owner_contact"
            target.evidence.append(f"Public phone found on strongly matching cross-post: {result.url}");target.evidence.append(f"Contact match score: {result.score}/100 ({', '.join(result.evidence)})");classify_seller(target)
        return listings
