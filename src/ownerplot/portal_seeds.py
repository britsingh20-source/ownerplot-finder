from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urlparse

import httpx

from .domain import Listing, SellerType

AREA_RE = re.compile(r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(?:sq\.?\s*ft|sqft|square feet)", re.I)
PRICE_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)\s*(crores?|cr|lakhs?|lacs?)", re.I)
OWNER_LABEL_RE = re.compile(r"\bowner\s*:\s*([A-Za-z][A-Za-z .]{1,60}?)(?=\s+(?:\+?91|₹|rs\.?|inr|plot|land|contact|\d{3,5}\s*(?:sq|square))|$)", re.I)
OWNER_PATTERNS = [
    OWNER_LABEL_RE,
    re.compile(r"\bcontact owner\b.*?\b([A-Z][A-Za-z .]{1,45})\s*\+?91", re.I),
]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _area(text: str) -> float | None:
    m = AREA_RE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


def _price(text: str) -> int | None:
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    value = float(m.group(1)); unit = m.group(2).lower()
    return int(value * (10_000_000 if unit.startswith(("cr", "crore")) else 100_000))


def _owner(text: str) -> str | None:
    for pattern in OWNER_PATTERNS:
        match = pattern.search(text or "")
        if match:
            value = " ".join(match.group(1).split()).strip(" .,-")
            if value.lower() not in {"owner", "contact owner", "individual"}:
                return value
    return None


def _detail_like(url: str, text: str) -> bool:
    host = _host(url); path = urlparse(url).path.lower(); lower = (text or "").lower()
    if host.endswith("magicbricks.com"):
        return "propertydetails" in path or ("contact owner" in lower and bool(AREA_RE.search(text)))
    if host.endswith("99acres.com"):
        return "-npffid" in path and not path.endswith("-ffid")
    return False


def _owner_cards(text: str) -> list[str]:
    """Split indexed MagicBricks category text into owner-labelled property cards."""
    matches=list(re.finditer(r"\bOwner\s*:\s*[A-Za-z]",text or "",re.I))
    cards=[]
    for i,match in enumerate(matches):
        start=max(0,match.start()-500); end=matches[i+1].start() if i+1<len(matches) else min(len(text),match.start()+3500)
        card=text[start:end]
        if AREA_RE.search(card) and re.search(r"\b(plot|land)\b",card,re.I): cards.append(card)
    return cards


class PortalOwnerSeedCollector:
    """Multi-engine discovery of MagicBricks/99acres owner plot seeds."""

    def __init__(self, api_key: str = "", cse_id: str = "", tavily_api_key: str = "", client: httpx.AsyncClient | None = None) -> None:
        self.api_key=api_key; self.cse_id=cse_id; self.tavily_api_key=tavily_api_key
        self.client=client or httpx.AsyncClient(timeout=20,follow_redirects=True,headers={"User-Agent":"OwnerPlotFinder/0.6 (+portal-owner-seeds)"})

    @classmethod
    def from_environment(cls):
        key=os.getenv("GOOGLE_CSE_API_KEY","").strip(); cse=os.getenv("GOOGLE_CSE_ID","").strip(); tavily=os.getenv("TAVILY_API_KEY","").strip()
        return cls(key,cse,tavily) if (key and cse) or tavily else None

    async def _google_search(self,q:str)->list[dict]:
        if not self.api_key or not self.cse_id:return []
        try:
            response=await self.client.get("https://www.googleapis.com/customsearch/v1",params={"key":self.api_key,"cx":self.cse_id,"q":q,"num":10}); response.raise_for_status()
            return [{"link":i.get("link",""),"title":i.get("title",""),"snippet":i.get("snippet","") or "","raw":"","engine":"google"} for i in response.json().get("items",[])]
        except (httpx.HTTPError,ValueError,KeyError):return []

    async def _tavily_search(self,q:str)->list[dict]:
        if not self.tavily_api_key:return []
        try:
            response=await self.client.post("https://api.tavily.com/search",headers={"Authorization":f"Bearer {self.tavily_api_key}","Content-Type":"application/json"},json={"query":q,"topic":"general","search_depth":"advanced","max_results":15,"include_answer":False,"include_raw_content":"text","include_images":False,"include_domains":["magicbricks.com","99acres.com"]}); response.raise_for_status()
            return [{"link":i.get("url",""),"title":i.get("title",""),"snippet":i.get("content","") or "","raw":i.get("raw_content","") or "","engine":"tavily"} for i in response.json().get("results",[])]
        except (httpx.HTTPError,ValueError,KeyError):return []

    async def _search(self,q:str)->list[dict]:
        google,tavily=await asyncio.gather(self._google_search(q),self._tavily_search(q)); seen=set(); out=[]
        for item in [*google,*tavily]:
            url=item.get("link","")
            key=(url,item.get("title",""))
            if url and key not in seen:seen.add(key);out.append(item)
        return out

    async def _fetch_public_text(self,url:str)->str:
        try:
            response=await self.client.get(url)
            if response.status_code>=400 or "text/html" not in response.headers.get("content-type","").lower():return ""
            body=re.sub(r"<script\b[^>]*>.*?</script>"," ",response.text,flags=re.I|re.S); body=re.sub(r"<style\b[^>]*>.*?</style>"," ",body,flags=re.I|re.S); body=re.sub(r"<[^>]+>"," ",body)
            return re.sub(r"\s+"," ",body)[:120000]
        except httpx.HTTPError:return ""

    def _listing(self,url:str,title:str,text:str,locality:str,evidence_source:str,synthetic:bool=False)->Listing|None:
        if locality.lower() not in text.lower() or not re.search(r"\b(plot|land|residential plot|residential land)\b",text,re.I):return None
        owner=_owner(text); owner_marker=bool(re.search(r"\b(contact owner|owner\s*:|posted by owner|owner property|individual)\b",text,re.I))
        if not owner_marker:return None
        area=_area(text)
        if not area:return None
        return Listing(source=_host(url),source_id=f"{url}#{owner or area}" if synthetic else url,url=url,title=title or "Owner plot listing",description=text[:25000],locality=locality,property_type="plot",price=_price(text),area_sqft=area,phone=None,phone_public=False,seller_claim=owner or "owner",seller_type=SellerType.PROBABLE_OWNER,owner_confidence=80 if owner else 65,locality_confidence=100,contact_verification="portal_owner_seed",evidence=[f"Owner seed discovered via {evidence_source}","Portal explicitly labels advertiser as owner","Protected portal contact not accessed"])

    async def search(self,locality:str)->list[Listing]:
        queries=[
            f'site:magicbricks.com "{locality}" Coimbatore plot "Owner:"',
            f'site:magicbricks.com/propertyDetails "{locality}" Coimbatore plot "Contact Owner"',
            f'site:99acres.com "{locality}" Coimbatore residential land owner',
            f'site:99acres.com "{locality}" Coimbatore plot "posted by owner"',
        ]
        output=[]; seen_detail=set(); synthetic_keys=set()
        for query in queries:
            for item in await self._search(query):
                url=item.get("link",""); host=_host(url)
                if not url or host not in {"magicbricks.com","99acres.com"}:continue
                snippet=" ".join(str(item.get(k,"")) for k in ("title","snippet","raw")); page=await self._fetch_public_text(url) if _detail_like(url,snippet) else ""; text=re.sub(r"\s+"," ",f"{snippet} {page}").strip()
                if _detail_like(url,text):
                    if url in seen_detail:continue
                    seed=self._listing(url,item.get("title") or "Owner plot listing",text,locality,f"{item.get('engine','search')} detail-page discovery")
                    if seed:seen_detail.add(url);output.append(seed)
                elif host=="magicbricks.com":
                    for card in _owner_cards(text):
                        owner=_owner(card); area=_area(card); key=(owner or "",round(area or 0),_price(card) or 0)
                        if key in synthetic_keys:continue
                        seed=self._listing(url,f"MagicBricks owner card — {owner or 'Owner'}",card,locality,f"{item.get('engine','search')} indexed owner-card discovery",synthetic=True)
                        if seed:synthetic_keys.add(key);output.append(seed)
        return output
