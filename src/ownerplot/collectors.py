from __future__ import annotations

import ipaddress, os, re, socket
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
try:
    import httpx
except ModuleNotFoundError:  # Parser tests can run before project dependencies are installed.
    httpx = None
from .domain import Listing, SearchQuery


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.title=""; self._in_title=False
    def handle_starttag(self, tag, attrs):
        if tag == "title": self._in_title=True
    def handle_endtag(self, tag):
        if tag == "title": self._in_title=False
    def handle_data(self, data):
        clean=" ".join(data.split())
        if clean:
            self.parts.append(clean)
            if self._in_title: self.title += clean


PHONE_RE=re.compile(r"(?:\+?91[\s.-]?)?([6-9](?:[\s.-]?\d){9})(?!\d)")
PRICE_RE=re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)\s*(crores?|cr|lakhs?|lacs?)", re.I)
AREA_RE=re.compile(r"([0-9,]+(?:\.[0-9]+)?)\s*(sq\.?\s*ft|sqft|square feet|cents?)", re.I)


def _price(text):
    m=PRICE_RE.search(text)
    if not m: return None
    return int(float(m.group(1))*(10_000_000 if m.group(2).lower().startswith(("cr","crore")) else 100_000))


def _area(text):
    m=AREA_RE.search(text)
    if not m: return None
    value=float(m.group(1).replace(",",""))
    return value*435.6 if m.group(2).lower().startswith("cent") else value


def _phone(text):
    m=PHONE_RE.search(text)
    return re.sub(r"\D","",m.group(1)) if m else None


def _public_ip(host):
    try: addresses=socket.getaddrinfo(host,None)
    except socket.gaierror: return False
    for item in addresses:
        ip=ipaddress.ip_address(item[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast: return False
    return True


class GooglePublicWebCollector:
    """Google discovery followed by collection from reviewed public domains only."""
    source_id="google_public_web"

    def __init__(self, api_key, cse_id, allowed_domains, client=None):
        if httpx is None:
            raise RuntimeError("Install project dependencies before enabling public web search")
        self.api_key,self.cse_id=api_key,cse_id
        self.allowed_domains={d.lower().removeprefix("www.") for d in allowed_domains if d}
        self.client=client or httpx.AsyncClient(timeout=12,follow_redirects=True,headers={"User-Agent":"OwnerPlotFinder/0.1 (+public-source-research)"})

    @classmethod
    def from_environment(cls):
        key,cse=os.getenv("GOOGLE_CSE_API_KEY",""),os.getenv("GOOGLE_CSE_ID","")
        domains={x.strip() for x in os.getenv("ALLOWED_PUBLIC_DOMAINS","").split(",") if x.strip()}
        if not domains:
            registry=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),"config","allowed-public-domains.txt")
            try:
                with open(registry,encoding="utf-8") as handle:
                    domains={line.strip() for line in handle if line.strip() and not line.startswith("#")}
            except OSError:
                pass
        return cls(key,cse,domains) if key and cse and domains else None

    def _allowed_url(self,url):
        parsed=urlparse(url); host=(parsed.hostname or "").lower().removeprefix("www.")
        return parsed.scheme=="https" and _public_ip(host) and any(host==d or host.endswith(f".{d}") for d in self.allowed_domains)

    async def _robots_allowed(self,url):
        parsed=urlparse(url); robots_url=f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            response=await self.client.get(robots_url)
            if response.status_code>=400: return False
            parser=RobotFileParser(); parser.set_url(robots_url); parser.parse(response.text.splitlines())
            return parser.can_fetch("OwnerPlotFinder",url)
        except httpx.HTTPError: return False

    async def search(self,query):
        terms=f'"{query.locality}" (plot OR land OR "house site") Coimbatore'
        response=await self.client.get("https://www.googleapis.com/customsearch/v1",params={"key":self.api_key,"cx":self.cse_id,"q":terms,"num":10})
        if response.is_error:
            try:
                error=response.json().get("error",{})
                detail=error.get("message") or error.get("status") or "unknown Google API error"
            except ValueError:
                detail="unknown Google API error"
            raise RuntimeError(f"Google Custom Search API failed ({response.status_code}): {detail}")
        output=[]
        for item in response.json().get("items",[]):
            url=item.get("link","")
            if not self._allowed_url(url): continue
            public_text=" ".join(filter(None,[item.get("title",""),item.get("snippet","")]))
            title=item.get("title","Public plot listing")
            evidence=[]
            if await self._robots_allowed(url):
                try:
                    page=await self.client.get(url); page.raise_for_status()
                    if "text/html" in page.headers.get("content-type",""):
                        parser=_TextExtractor(); parser.feed(page.text[:2_000_000])
                        public_text += " " + " ".join(parser.parts)
                        title=parser.title or title
                        evidence.append("Public source page fetched with robots permission")
                except httpx.HTTPError:
                    pass
            phone=_phone(public_text)
            if phone:
                evidence.append("Contact visibly present in public page or indexed result metadata")
            output.append(Listing(source=urlparse(url).hostname or "public-web",source_id=item.get("cacheId") or url,url=url,title=title,description=public_text[:20_000],locality=query.locality,property_type="plot" if re.search(r"\b(plot|land|site)\b",public_text,re.I) else "unknown",price=_price(public_text),area_sqft=_area(public_text),phone=phone,phone_public=bool(phone),seller_claim="owner" if re.search(r"\b(owner|no brokerage|direct owner)\b",public_text,re.I) else None,evidence=evidence))
        return output


class TavilyPublicWebCollector:
    """Tavily discovery constrained to the reviewed public-domain registry."""
    source_id="tavily_public_web"

    def __init__(self,api_key,allowed_domains,client=None):
        if httpx is None:
            raise RuntimeError("Install project dependencies before enabling Tavily search")
        self.api_key=api_key
        self.allowed_domains={d.lower().removeprefix("www.") for d in allowed_domains if d}
        self.client=client or httpx.AsyncClient(timeout=45,follow_redirects=True,headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json","User-Agent":"OwnerPlotFinder/0.1"})

    @classmethod
    def from_environment(cls):
        key=os.getenv("TAVILY_API_KEY","").strip()
        domains={x.strip() for x in os.getenv("ALLOWED_PUBLIC_DOMAINS","").split(",") if x.strip()}
        if not domains:
            registry=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),"config","allowed-public-domains.txt")
            try:
                with open(registry,encoding="utf-8") as handle:
                    domains={line.strip() for line in handle if line.strip() and not line.startswith("#")}
            except OSError:
                pass
        return cls(key,domains) if key and domains else None

    async def search(self,query):
        terms=f'Find public advertisements for plots or land for sale in {query.locality}, Coimbatore, prioritizing direct-owner or no-brokerage posts that visibly publish a phone, call, mobile or WhatsApp contact number.'
        response=await self.client.post("https://api.tavily.com/search",json={"query":terms,"topic":"general","search_depth":"basic","max_results":20,"include_answer":False,"include_raw_content":"text","include_images":False,"include_domains":sorted(self.allowed_domains)})
        if response.is_error:
            try:
                detail=response.json().get("detail") or response.json().get("error") or response.text[:300]
            except ValueError:
                detail="unknown Tavily API error"
            raise RuntimeError(f"Tavily Search API failed ({response.status_code}): {detail}")
        output=[]
        for item in response.json().get("results",[]):
            url=item.get("url",""); parsed=urlparse(url); host=(parsed.hostname or "").lower().removeprefix("www.")
            if parsed.scheme!="https" or not any(host==d or host.endswith(f".{d}") for d in self.allowed_domains): continue
            text=" ".join(filter(None,[item.get("title",""),item.get("content",""),item.get("raw_content","")]))[:20_000]
            phone=_phone(text)
            output.append(Listing(source=host or "tavily",source_id=url,url=url,title=item.get("title") or "Public property listing",description=text,locality=query.locality,property_type="plot" if re.search(r"\b(plot|land|site)\b",text,re.I) else "unknown",price=_price(text),area_sqft=_area(text),phone=phone,phone_public=bool(phone),seller_claim="owner" if re.search(r"\b(owner|no brokerage|direct owner|individual)\b",text,re.I) else None,evidence=["Contact visibly present in Tavily-extracted public source content"] if phone else ["Public source discovered through Tavily"]))
        return output


class EmptyCollector:
    source_id="safe_default"
    async def search(self,query): return []
