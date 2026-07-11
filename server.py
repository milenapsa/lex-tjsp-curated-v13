
from __future__ import annotations
import html, json, os, re, time, urllib.parse, urllib.request, uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT=int(os.getenv("PORT","8080"))
UPSTREAM=os.getenv("LEX_UPSTREAM","http://homosapiens-lex-tjpr-curated-v12:8080")
VERSION="0.13.0-tjsp-curated"
TTL=1800
UA="Lex-HomoSapiens/0.13"
COMESP="https://portal.tjsp.jus.br/Comesp/Enunciados"
TEMAS="https://portal.tjsp.jus.br/SecaoDireitoPrivado/PesquisasTematicas/Pesquisas"
PORTAL="https://www.tjsp.jus.br/jurisprudencia"
_cache={}

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(); self.text=[]; self.links=[]; self.skip=0
    def handle_starttag(self,tag,attrs):
        d=dict(attrs)
        if tag in {"script","style","nav","header","footer"}: self.skip+=1
        if tag=="a" and d.get("href"): self.links.append((d.get("href"),""))
        if tag in {"p","li","h1","h2","h3","h4","div","br","td"} and not self.skip: self.text.append("\n")
    def handle_endtag(self,tag):
        if tag in {"script","style","nav","header","footer"} and self.skip: self.skip-=1
        if tag in {"p","li","h1","h2","h3","h4","div","td"} and not self.skip: self.text.append("\n")
    def handle_data(self,data):
        if not self.skip:
            self.text.append(data)
            if self.links and not self.links[-1][1]:
                href,_=self.links[-1]; self.links[-1]=(href,data.strip())
            elif self.links and data.strip():
                href,txt=self.links[-1]; self.links[-1]=(href,(txt+" "+data.strip()).strip())

def fetch_html(url):
    hit=_cache.get(url)
    if hit and time.time()-hit[0] < TTL: return hit[1]
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"text/html"})
    with urllib.request.urlopen(req,timeout=25) as r:
        body=r.read(2_500_000).decode("utf-8","replace")
    p=Parser(); p.feed(body)
    text=html.unescape(re.sub(r"[ \t]+"," ","".join(p.text)))
    text=re.sub(r"\n{2,}","\n",text)
    links=[]
    for href,txt in p.links:
        links.append((urllib.parse.urljoin(url,html.unescape(href)),html.unescape(txt)))
    _cache[url]=(time.time(),(text,links))
    return text,links

STOP={"de","da","do","das","dos","e","a","o","em","para","por","com","um","uma","no","na","nos","nas","lei","art"}
def tokens(q):
    return [x for x in re.findall(r"[a-z0-9áéíóúâêôãõç]+",q.lower()) if len(x)>2 and x not in STOP]

def comesp_search(query,limit):
    toks=tokens(query); results=[]; evidence=[]
    # first 3 pages to control load
    for page in range(1,4):
        url=COMESP if page==1 else f"{COMESP}?pagina={page}&tipoDestino=202"
        text,links=fetch_html(url)
        pat=re.compile(r"ENUNCIADO\s+(?P<num>\d+)\s*:\s*(?P<body>.*?)(?=(?:\n|\s)ENUNCIADO\s+\d+\s*:|$)",re.I|re.S)
        count=0
        for m in pat.finditer(text):
            body=re.sub(r"\s+"," ",m.group("body")).strip()
            if len(body)<25: continue
            low=body.lower()
            score=sum(1 for t in toks if t in low)
            if score and (len(toks)<=1 or score>=min(2,len(toks))):
                num=m.group("num")
                detail=None
                for href,txt in links:
                    if f"590{num}" in href or re.search(rf"\b{num}\b",txt or ""):
                        detail=href; break
                results.append({
                    "id":f"tjsp-comesp:{num}",
                    "title":f"TJSP — COMESP — Enunciado {num}",
                    "summary":body[:1400],
                    "type":"enunciado_tjsp_comesp",
                    "date":"",
                    "organization":"Tribunal de Justiça do Estado de São Paulo",
                    "source":"tjsp_comesp_enunciados",
                    "source_label":"TJSP — COMESP — Enunciados",
                    "source_url":url,
                    "official_url":detail or url,
                    "is_official":True,
                    "is_synthetic":False,
                    "retrieved_at":now(),
                    "match_score":score
                }); count+=1
        evidence.append({"source":"tjsp_comesp_enunciados","status":"ok","count":count,"request_url":url,"cache_ttl_seconds":TTL})
    # dedup
    seen=set(); out=[]
    for x in sorted(results,key=lambda z:(-z["match_score"],int(re.findall(r"\d+",z["id"])[0]))):
        if x["id"] not in seen:
            seen.add(x["id"]); out.append(x)
    return out[:limit], evidence

def temas_search(query,limit):
    toks=tokens(query); results=[]
    _,links=fetch_html(TEMAS)
    seen=set()
    for href,txt in links:
        if ".pdf" not in href.lower(): continue
        label=re.sub(r"\s+"," ",txt).strip()
        if not label:
            label=urllib.parse.unquote(href.rsplit("/",1)[-1].rsplit(".",1)[0])
            label=re.sub(r"[_-]+"," ",label)
        low=(label+" "+href).lower()
        score=sum(1 for t in toks if t in low)
        if not score or (len(toks)>1 and score<min(2,len(toks))): continue
        if href in seen: continue
        seen.add(href)
        results.append({
            "id":"tjsp-tema:"+str(abs(hash(href))),
            "title":"TJSP — Pesquisa Temática — "+label[:180],
            "summary":"Pesquisa temática oficial da Seção de Direito Privado do TJSP. Consulte o PDF oficial para os precedentes e a data de atualização.",
            "type":"pesquisa_tematica_tjsp",
            "date":"",
            "organization":"Tribunal de Justiça do Estado de São Paulo",
            "source":"tjsp_pesquisas_tematicas",
            "source_label":"TJSP — Pesquisas Temáticas da Seção de Direito Privado",
            "source_url":TEMAS,
            "official_url":href,
            "is_official":True,
            "is_synthetic":False,
            "retrieved_at":now(),
            "match_score":score
        })
    results.sort(key=lambda z:(-z["match_score"],z["title"]))
    return results[:limit], [{"source":"tjsp_pesquisas_tematicas","status":"ok","count":min(len(results),limit),"request_url":TEMAS,"cache_ttl_seconds":TTL}]

def fetch_json(url,method="GET",payload=None):
    body=None if payload is None else json.dumps(payload,ensure_ascii=False).encode()
    headers={"User-Agent":UA,"Accept":"application/json"}
    if body is not None: headers["Content-Type"]="application/json"
    req=urllib.request.Request(url,data=body,headers=headers,method=method)
    with urllib.request.urlopen(req,timeout=25) as r:
        return json.loads(r.read().decode())

def interleave(items,limit):
    groups=defaultdict(deque); order=[]
    for x in items:
        s=x.get("source","unknown")
        if s not in groups: order.append(s)
        groups[s].append(x)
    out=[]
    while len(out)<limit and any(groups[s] for s in order):
        for s in order:
            if groups[s] and len(out)<limit: out.append(groups[s].popleft())
    return out

SOURCES=[
 {"id":"tjsp_comesp_enunciados","name":"TJSP — COMESP — Enunciados","status":"online","coverage":["violencia_domestica","medidas_protetivas","genero"],"official":True,"requires_secret":False,"url":COMESP},
 {"id":"tjsp_pesquisas_tematicas","name":"TJSP — Pesquisas Temáticas da Seção de Direito Privado","status":"online","coverage":["direito_privado","pesquisas_tematicas","precedentes_em_pdf"],"official":True,"requires_secret":False,"url":TEMAS},
 {"id":"tjsp_portal_jurisprudencia","name":"TJSP — Portal de Jurisprudência eproc/SAJ","status":"manual_official_portal","coverage":["acordaos","decisoes","ementas"],"official":True,"requires_secret":False,"url":PORTAL,"automation_note":"Portal integral mantido como consulta oficial separada; conector automático usa apenas fontes curadas."}
]

def run_search(path,payload):
    started=time.monotonic()
    q=str(payload.get("query") or payload.get("q") or "").strip()
    limit=max(1,min(int(payload.get("limit",10)),20))
    base=fetch_json(UPSTREAM+("/v1/search" if path=="/v1/search" else path),"POST",payload)
    results=list(base.get("results") or []); evidence=list(base.get("evidence") or [])
    for fn in (comesp_search,temas_search):
        try:
            found,proof=fn(q,limit); results.extend(found); evidence.extend(proof)
        except Exception as exc:
            evidence.append({"source":"tjsp_curated","status":"error","error_type":exc.__class__.__name__,"message":str(exc)[:200]})
    seen=set(); dedup=[]
    for x in results:
        k=(x.get("source"),x.get("id"),x.get("title"))
        if k in seen: continue
        seen.add(k); dedup.append(x)
    final=interleave(dedup,limit)
    return {
      "status":"ok","service":"lex-search-aggregator","version":VERSION,"generated_at":now(),
      "trace_id":str(uuid.uuid4()),"query":q,"scope":base.get("scope","all"),
      "result_count":len(final),"results":final,"evidence":evidence,
      "sources_used":sorted({x.get("source") for x in final if x.get("source")}),
      "integrity":{"official":sum(1 for x in final if x.get("is_official")),
                   "synthetic":sum(1 for x in final if x.get("is_synthetic")),
                   "source_urls_present":sum(1 for x in final if x.get("source_url"))},
      "warnings":list(base.get("warnings") or []),
      "human_review_required":True,"no_invention_policy":True,
      "duration_ms":int((time.monotonic()-started)*1000)
    }

class H(BaseHTTPRequestHandler):
    def sendj(self,status,obj):
        data=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store")
        self.end_headers(); self.wfile.write(data)
    def body(self):
        n=int(self.headers.get("Content-Length","0") or 0)
        if n>64000: raise ValueError("payload_too_large")
        return json.loads((self.rfile.read(n) if n else b"{}").decode())
    def do_GET(self):
        p=urllib.parse.urlparse(self.path).path
        online=["camara_proposicoes","senado_processos","senado_legislacao","tse_ckan","tjsc_sumulas","tjsc_enunciados","tjrs_sumulas_tr_fazenda","tjpr_enunciados_turmas","tjpr_enunciados_tuj","tjsp_comesp_enunciados","tjsp_pesquisas_tematicas"]
        if p in {"/health","/v1/health"}:
            return self.sendj(200,{"status":"ok","service":"lex-search-aggregator","version":VERSION,"generated_at":now(),"real_sources_online":online,"human_review_required":True,"no_invention_policy":True})
        if p in {"/ready","/v1/readiness"}:
            return self.sendj(200,{"status":"ready","version":VERSION,"online_sources":online,"generated_at":now()})
        if p in {"/v1/sources","/v1/sources/registry"}:
            base=fetch_json(UPSTREAM+"/v1/sources")
            return self.sendj(200,{"status":"ok","service":"lex-search-aggregator","version":VERSION,"generated_at":now(),"sources":list(base.get("sources") or [])+SOURCES,"human_review_required":True,"no_invention_policy":True})
        self.sendj(404,{"error":"not_found"})
    def do_POST(self):
        p=urllib.parse.urlparse(self.path).path
        if p not in {"/v1/search","/v1/search/global","/v1/search/legislacao","/v1/search/datasets"}:
            return self.sendj(404,{"error":"not_found"})
        try:
            payload=self.body()
            if not str(payload.get("query") or payload.get("q") or "").strip():
                return self.sendj(422,{"error":"query_required"})
            self.sendj(200,run_search(p,payload))
        except Exception as exc:
            self.sendj(500,{"error":"tjsp_curated_connector_error","detail":exc.__class__.__name__})
    def log_message(self,*args): pass

ThreadingHTTPServer(("0.0.0.0",PORT),H).serve_forever()
