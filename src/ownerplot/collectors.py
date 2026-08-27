from __future__ import annotations

import asyncio, ipaddress, os, re, socket
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree
import yaml
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
        values=dict(attrs)
        if tag == "a" and values.get("href"):
            href=values["href"]
            if href.startswith("tel:") or "wa.me/" in href or "api.whatsapp.com/" in href:
                self.parts.append(href)
        if tag == "meta" and values.get("content") and values.get("property") in {"og:description","twitter:description"}:
            self.parts.append(values["content"])
    def handle_endtag(self, tag):
        if tag == "title": self._in_title=False
    def handle_data(self, data):
        clean=" ".join(data.split())
        if clean:
            self.parts.append(clean)
            if self._in_title: self.title += clean


PHONE_RE=re.compile(r"(?:\+?91[\s.-]?)?([6-9](?:[\s.-]?\d){9})(?!\d)")
PRICE_RE=re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)\s*(crores?|cr|lakhs?|lacs?)", re.I)
AREA_RE=re.compile(r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(sq\.?\s*ft|sqft|square feet|cents?)", re.I)
DATE_LABEL_RE=re.compile(
    r"\b(?:posted|published|updated|listed)(?:\s+on|\s*:)?\s*"
    r"(today|yesterday|\d{1,2}\s+(?:days?|weeks?|months?)\s+ago|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}(?:,)?\s+\d{4})\b",
    re.I,
)


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


def _original_post_date(text, published_date=None, now=None):
    """Return (UTC datetime, confidence, evidence) from original-source metadata/text."""
    now=now or datetime.now(timezone.utc)
    candidates=[]
    if published_date:
        candidates.append((str(published_date).strip(),95,"Source metadata publish date"))
    match=DATE_LABEL_RE.search(text or "")
    if match:
        candidates.append((match.group(1).strip(),80,"Original page labelled post/update date"))
    for raw,confidence,evidence in candidates:
        lowered=raw.lower()
        try:
            if lowered=="today": value=now
            elif lowered=="yesterday": value=now-timedelta(days=1)
            elif relative:=re.fullmatch(r"(\d{1,2})\s+(day|week|month)s?\s+ago",lowered):
                amount=int(relative.group(1)); unit=relative.group(2)
                value=now-timedelta(days=amount*(1 if unit=="day" else 7 if unit=="week" else 30))
            else:
                normalized=raw.replace("Z","+00:00")
                try: value=datetime.fromisoformat(normalized)
                except ValueError:
                    try: value=parsedate_to_datetime(raw)
                    except (TypeError,ValueError): value=None
                    for fmt in ("%d/%m/%Y","%d-%m-%Y","%b %d, %Y","%B %d, %Y","%b %d %Y","%B %d %Y"):
                        if value is not None: break
                        try: value=datetime.strptime(raw,fmt); break
                        except ValueError: pass
                    if value is None: continue
            if value.tzinfo is None: value=value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc),confidence,evidence
        except (OverflowError,ValueError):
            continue
    return None,0,"Original post date unavailable"


def _xml_entries(xml_text):
    """Parse RSS, Atom, sitemap and sitemap-index XML without trusting namespaces."""
    try: root=ElementTree.fromstring(xml_text)
    except ElementTree.ParseError: return []
    local=lambda tag: tag.rsplit("}",1)[-1].lower()
    output=[]
    root_kind=local(root.tag)
    if root_kind in {"urlset","sitemapindex"}:
        child_kind="sitemap" if root_kind=="sitemapindex" else "page"
        for node in root:
            values={local(child.tag):("".join(child.itertext()).strip()) for child in node}
            if values.get("loc"):
                output.append({"kind":child_kind,"url":values["loc"],"title":"","description":"","published":values.get("lastmod")})
        return output
    for node in root.iter():
        if local(node.tag) not in {"item","entry"}: continue
        values={}
        for child in node:
            name=local(child.tag)
            if name=="link": values[name]=child.attrib.get("href") or (child.text or "")
            else: values[name]=" ".join("".join(child.itertext()).split())
        url=values.get("link") or values.get("guid")
        if url:
            output.append({"kind":"page","url":url,"title":values.get("title",""),"description":values.get("description") or values.get("summary") or values.get("content","") ,"published":values.get("published") or values.get("updated") or values.get("pubdate")})
    return output


_YOUTUBE_CACHE={}


def _youtube_video_id(url):
    parsed=urlparse(url)
    host=(parsed.hostname or "").lower().removeprefix("www.")
    if host=="youtu.be": return parsed.path.strip("/").split("/")[0] or None
    if host not in {"youtube.com","m.youtube.com"}: return None
    if parsed.path=="/watch":
        from urllib.parse import parse_qs
        return (parse_qs(parsed.query).get("v") or [None])[0]
    match=re.match(r"/(?:shorts|embed)/([^/?]+)",parsed.path)
    return match.group(1) if match else None


async def _enrich_youtube_descriptions(listings,client):
    """Use the official API to read public video descriptions; never accesses private data."""
    api_key=os.getenv("YOUTUBE_API_KEY","").strip()
    keyed={video_id:item for item in listings if (video_id:=_youtube_video_id(item.url))}
    if not keyed or not api_key: return listings
    missing=[video_id for video_id in keyed if video_id not in _YOUTUBE_CACHE]
    for start in range(0,len(missing),50):
        batch=missing[start:start+50]
        try:
            response=await client.get("https://www.googleapis.com/youtube/v3/videos",params={"key":api_key,"part":"snippet","id":",".join(batch)})
            response.raise_for_status()
            returned={item["id"]:item.get("snippet",{}) for item in response.json().get("items",[])}
            for video_id in batch: _YOUTUBE_CACHE[video_id]=returned.get(video_id)
        except (httpx.HTTPError,ValueError,KeyError):
            for video_id in batch: _YOUTUBE_CACHE[video_id]=None
    for video_id,item in keyed.items():
        snippet=_YOUTUBE_CACHE.get(video_id)
        if not snippet: continue
        description=snippet.get("description","")
        channel=snippet.get("channelTitle","")
        item.description=f"{item.description} {description} Channel: {channel}"[:20_000]
        item.title=snippet.get("title") or item.title
        if phone:=_phone(description):
            item.phone=phone; item.phone_public=True
            item.evidence.append("Contact visibly published in official YouTube video description")
        if not item.original_posted_at:
            posted,confidence,evidence=_original_post_date(item.description,snippet.get("publishedAt"))
            item.original_posted_at=posted; item.date_confidence=confidence
            item.date_status="verified_recent" if posted else item.date_status
            item.evidence.append(evidence)
        if re.search(r"\b(owner|direct owner|no brokerage|individual)\b",description,re.I): item.seller_claim="owner"
    return listings


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
        return await _enrich_youtube_descriptions(output,self.client)


class YouTubePublicSearchCollector:
    """Official-API discovery of recent public property videos and descriptions."""
    source_id="youtube_public_search"

    def __init__(self,api_key,client=None):
        if httpx is None: raise RuntimeError("Install project dependencies before enabling YouTube search")
        self.api_key=api_key
        self.client=client or httpx.AsyncClient(timeout=30,follow_redirects=True,headers={"User-Agent":"OwnerPlotFinder/0.3"})

    @classmethod
    def from_environment(cls):
        key=os.getenv("YOUTUBE_API_KEY","").strip()
        return cls(key) if key else None

    async def search(self,query):
        cutoff=datetime.now(timezone.utc)-timedelta(days=query.max_age_days)
        response=await self.client.get("https://www.googleapis.com/youtube/v3/search",params={"key":self.api_key,"part":"snippet","type":"video","maxResults":25,"order":"date","q":f'{query.locality} Coimbatore plot land for sale',"publishedAfter":cutoff.isoformat().replace("+00:00","Z"),"regionCode":"IN"})
        if response.is_error:
            try: detail=response.json().get("error",{}).get("message") or response.text[:300]
            except ValueError: detail="unknown YouTube API error"
            raise RuntimeError(f"YouTube Data API failed ({response.status_code}): {detail}")
        output=[]
        for result in response.json().get("items",[]):
            video_id=(result.get("id") or {}).get("videoId")
            snippet=result.get("snippet") or {}
            if not video_id: continue
            title=snippet.get("title",""); description=snippet.get("description","")
            text=f"{title} {description} Channel: {snippet.get('channelTitle','')}"
            if query.locality.lower() not in text.lower() or not re.search(r"\b(plot|land|site|residential)\b",text,re.I): continue
            posted,confidence,evidence=_original_post_date(text,snippet.get("publishedAt"))
            phone=_phone(text)
            output.append(Listing(source="youtube.com",source_id=video_id,url=f"https://www.youtube.com/watch?v={video_id}",title=title or "Public property video",description=text,locality=query.locality,property_type="plot",price=_price(text),area_sqft=_area(text),phone=phone,phone_public=bool(phone),seller_claim="owner" if re.search(r"\b(owner|direct owner|no brokerage|individual)\b",text,re.I) else None,original_posted_at=posted,date_confidence=confidence,date_status="verified_recent" if posted else "unverified",evidence=["Official YouTube keyword search",evidence]+(["Contact visibly published in public YouTube metadata"] if phone else [])))
        return await _enrich_youtube_descriptions(output,self.client)


class TavilyPublicWebCollector:
    """Tavily discovery constrained to the reviewed public-domain registry."""
    source_id="tavily_public_web"

    PROFILES={
        "public_contacts":({"youtube.com","facebook.com","instagram.com"},"Find public social posts or videos advertising plots or land for sale in {locality}, Coimbatore that visibly publish a direct-owner phone, mobile, call or WhatsApp contact number."),
        "portals":({"olx.in","quikr.com","commonfloor.com","property.sulekha.com","roofandfloor.com","housing.com","magicbricks.com","99acres.com","nobroker.in"},"Find current plots or land for sale in {locality}, Coimbatore, prioritizing owner-posted or no-brokerage listings and visibly published contact details."),
        "local_sites":({"adissia.com","greenfieldcoimbatore.com","srisasthabuilders.com","livingspacerealty.com"},"Find current public advertisements for plots or land for sale in {locality}, Coimbatore, including visible phone or WhatsApp contacts and seller identity."),
    }
    DISCOVERY_ONLY={"magicbricks.com","99acres.com","nobroker.in","housing.com"}

    def __init__(self,api_key,allowed_domains,profile="public_contacts",client=None):
        if httpx is None:
            raise RuntimeError("Install project dependencies before enabling Tavily search")
        self.api_key=api_key
        self.allowed_domains={d.lower().removeprefix("www.") for d in allowed_domains if d}
        if profile not in self.PROFILES:
            raise ValueError(f"Unknown Tavily search profile: {profile}")
        self.profile=profile
        self.client=client or httpx.AsyncClient(timeout=45,follow_redirects=True,headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json","User-Agent":"OwnerPlotFinder/0.1"})

    @classmethod
    def from_environment(cls,profile="public_contacts"):
        key=os.getenv("TAVILY_API_KEY","").strip()
        domains={x.strip() for x in os.getenv("ALLOWED_PUBLIC_DOMAINS","").split(",") if x.strip()}
        if not domains:
            registry=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),"config","allowed-public-domains.txt")
            try:
                with open(registry,encoding="utf-8") as handle:
                    domains={line.strip() for line in handle if line.strip() and not line.startswith("#")}
            except OSError:
                pass
        return cls(key,domains,profile=profile) if key and domains else None

    async def search(self,query):
        profile_domains,template=self.PROFILES[self.profile]
        domains=sorted(self.allowed_domains & profile_domains)
        cutoff=(datetime.now(timezone.utc)-timedelta(days=query.max_age_days)).date().isoformat()
        terms=template.format(locality=query.locality)+f" Only include advertisements posted or updated within the last {query.max_age_days} days."
        response=await self.client.post("https://api.tavily.com/search",json={"query":terms,"topic":"general","search_depth":"basic","max_results":20,"start_date":cutoff,"include_answer":False,"include_raw_content":"text","include_images":False,"include_domains":domains})
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
            posted_at,date_confidence,date_evidence=_original_post_date(text,item.get("published_date"))
            phone=_phone(text)
            if host in self.DISCOVERY_ONLY:
                phone=None
            evidence=[f"Tavily discovery cutoff: last {query.max_age_days} days",date_evidence]
            evidence.append("Contact visibly present in Tavily-extracted public source content" if phone else "Public source discovered through Tavily")
            output.append(Listing(source=host or "tavily",source_id=url,url=url,title=item.get("title") or "Public property listing",description=text,locality=query.locality,property_type="plot" if re.search(r"\b(plot|land|site)\b",text,re.I) else "unknown",price=_price(text),area_sqft=_area(text),phone=phone,phone_public=bool(phone),seller_claim="owner" if re.search(r"\b(owner|no brokerage|direct owner|individual)\b",text,re.I) else None,original_posted_at=posted_at,date_confidence=date_confidence,date_status="verified_recent" if posted_at and posted_at.date()>=datetime.fromisoformat(cutoff).date() else "expired" if posted_at else "unverified",evidence=evidence))
        return await _enrich_youtube_descriptions(output,self.client)


class DirectPublicFeedCollector:
    """Zero-credit RSS/sitemap discovery from explicitly reviewed public sources."""
    source_id="direct_public_feeds"

    def __init__(self,sources,client=None,max_pages_per_source=30):
        if httpx is None:
            raise RuntimeError("Install project dependencies before enabling direct feeds")
        self.sources=[source for source in sources if source.get("enabled")]
        self.max_pages_per_source=max_pages_per_source
        self._robots_cache={}
        self._feed_cache={}
        self._page_cache={}
        self.client=client or httpx.AsyncClient(timeout=20,follow_redirects=True,headers={"User-Agent":"OwnerPlotFinder/0.2 (+public-feed-monitoring)"})

    @classmethod
    def from_environment(cls):
        path=Path(os.getenv("DIRECT_SOURCES_CONFIG",Path(__file__).resolve().parents[2]/"config"/"direct-sources.yaml"))
        try: payload=yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError,yaml.YAMLError): return None
        sources=payload.get("sources") or []
        return cls(sources) if any(source.get("enabled") for source in sources) else None

    @staticmethod
    def _host_allowed(url,domains):
        parsed=urlparse(url); host=(parsed.hostname or "").lower().removeprefix("www.")
        return parsed.scheme=="https" and any(host==domain or host.endswith(f".{domain}") for domain in domains)

    async def _robots_allowed(self,url):
        parsed=urlparse(url); robots_url=f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        cached=self._robots_cache.get(robots_url)
        if cached is not None:
            return cached is True or cached.can_fetch("OwnerPlotFinder",url)
        try:
            response=await self.client.get(robots_url)
            if response.status_code in {401,403}:
                self._robots_cache[robots_url]=False; return False
            if response.status_code==404:
                self._robots_cache[robots_url]=True; return True
            if response.status_code>=400:
                self._robots_cache[robots_url]=False; return False
            parser=RobotFileParser(); parser.set_url(robots_url); parser.parse(response.text.splitlines())
            self._robots_cache[robots_url]=parser
            return parser.can_fetch("OwnerPlotFinder",url)
        except httpx.HTTPError:
            self._robots_cache[robots_url]=False; return False

    async def _feed_entries(self,source):
        cache_key=source.get("id") or repr(source.get("urls",[]))
        if cache_key in self._feed_cache: return self._feed_cache[cache_key]
        domains={value.lower().removeprefix("www.") for value in source.get("allowed_domains",[])}
        pending=[(url,source.get("kind","rss")) for url in source.get("urls",[]) if self._host_allowed(url,domains)]
        pages=[]; visited=set()
        while pending and len(visited)<10:
            url,kind=pending.pop(0)
            if url in visited: continue
            visited.add(url)
            try:
                response=await self.client.get(url); response.raise_for_status()
            except httpx.HTTPError: continue
            for entry in _xml_entries(response.text):
                if not self._host_allowed(entry["url"],domains): continue
                if entry["kind"]=="sitemap": pending.append((entry["url"],"sitemap"))
                else: pages.append(entry)
        self._feed_cache[cache_key]=pages[:self.max_pages_per_source]
        return self._feed_cache[cache_key]

    async def search(self,query):
        cutoff=datetime.now(timezone.utc)-timedelta(days=query.max_age_days)
        wanted=set(re.findall(r"[a-z0-9]+",query.locality.lower()))
        feed_batches=await asyncio.gather(*(self._feed_entries(source) for source in self.sources),return_exceptions=True)
        semaphore=asyncio.Semaphore(5)

        async def process(source,entry):
          async with semaphore:
            domains={value.lower().removeprefix("www.") for value in source.get("allowed_domains",[])}
            metadata_text=" ".join([entry.get("title", ""),entry.get("description", ""),entry["url"]])
            posted,date_confidence,date_evidence=_original_post_date(metadata_text,entry.get("published"))
            if posted and posted<cutoff: return None
            metadata_tokens=set(re.findall(r"[a-z0-9]+",metadata_text.lower()))
            if wanted and wanted<=metadata_tokens:
                page_text=metadata_text; title=entry.get("title") or "Public property update"
            else:
                cached=self._page_cache.get(entry["url"],...)
                if cached is ...:
                    if not await self._robots_allowed(entry["url"]):
                        self._page_cache[entry["url"]]=None; return None
                    try:
                        page=await self.client.get(entry["url"]); page.raise_for_status()
                    except httpx.HTTPError:
                        self._page_cache[entry["url"]]=None; return None
                    final=str(page.url)
                    if not self._host_allowed(final,domains) or urlparse(final).path in {"","/"} or "text/html" not in page.headers.get("content-type",""):
                        self._page_cache[entry["url"]]=None; return None
                    parser=_TextExtractor(); parser.feed(page.text[:2_000_000])
                    cached=(" ".join(parser.parts)[:20_000],parser.title)
                    self._page_cache[entry["url"]]=cached
                if cached is None: return None
                page_text,page_title=cached; title=page_title or entry.get("title") or "Public property update"
            if not wanted<=set(re.findall(r"[a-z0-9]+",page_text.lower())): return None
            if not re.search(r"\b(plot|land|site|residential)\b",page_text,re.I): return None
            phone=_phone(page_text)
            evidence=[f"Zero-credit direct {source.get('kind','feed')} source",date_evidence,"Exact public source URL retained"]
            if phone: evidence.append("Contact visibly published on direct source")
            return Listing(source=source["id"],source_id=entry["url"],url=entry["url"],title=title,description=page_text,locality=query.locality,property_type="plot",price=_price(page_text),area_sqft=_area(page_text),phone=phone,phone_public=bool(phone),seller_claim="owner" if re.search(r"\b(owner|no brokerage|direct owner|individual)\b",page_text,re.I) else None,original_posted_at=posted,date_confidence=date_confidence,date_status="verified_recent" if posted else "unverified",evidence=evidence)

        tasks=[]
        for source,batch in zip(self.sources,feed_batches):
            if isinstance(batch,BaseException): continue
            tasks.extend(process(source,entry) for entry in batch)
        results=await asyncio.gather(*tasks,return_exceptions=True)
        return await _enrich_youtube_descriptions([item for item in results if isinstance(item,Listing)],self.client)


class EmptyCollector:
    source_id="safe_default"
    async def search(self,query): return []
