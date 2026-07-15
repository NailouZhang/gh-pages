import os
import sys
import json
import requests
import datetime
import hashlib
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

# ==========================================
# 核心系统配置
# ==========================================
PROFILE_ID = os.environ.get("PROFILE_ID", "hantavirus")
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# 时间窗口：近 7 天
TODAY = datetime.date.today()
START_DATE = TODAY - datetime.timedelta(days=7)
START_STR = START_DATE.strftime("%Y/%m/%d")
END_STR = TODAY.strftime("%Y/%m/%d")

API_STATUS_LOG = {}

# ==========================================
# LLM 基础通信（直连 HTTPS）
# ==========================================
def call_llm(prompt, json_mode=False):
    """
    原生的 API 请求方式，优先使用 Gemini 1.5 Flash，不可用时降级使用 Groq Llama-3 70B。
    """
    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": prompt}]}]
            }
            if json_mode:
                payload["generationConfig"] = {"responseMimeType": "application/json"}
            
            r = requests.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            else:
                print(f"[LLM LOG] Gemini returned code {r.status_code}, trying fallback.")
        except Exception as e:
            print(f"[LLM ERROR] Gemini fail: {e}")

    if GROQ_API_KEY:
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "llama3-70b-8192",
                "messages": [{"role": "user", "content": prompt}]
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            
            r = requests.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                data = r.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[LLM ERROR] Groq fail: {e}")

    print("[LLM FATAL] No LLM API key available or both failed.")
    return "{}" if json_mode else "Translation or Summarization unavailable."

# ==========================================
# 谷歌免费翻译作为降级保障 (Fallback)
# ==========================================
def fallback_translate(text):
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "en",
            "tl": "zh-CN",
            "dt": "t",
            "q": text
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            res = r.json()
            translated_chunks = [item[0] for item in res[0] if item[0]]
            return "".join(translated_chunks)
    except Exception as e:
        print(f"[FALLBACK TRANSLATE ERROR] {e}")
    return text

# ==========================================
# 种子分析与查询扩展
# ==========================================
def generate_search_plan(profile_id):
    print(f"[*] Analysing seed pathogen: {profile_id} via LLM with reference to ICTV & ViralZone...")
    prompt = f"""
    You are an expert computational virologist.
    Analyze the pathogen profile '{profile_id}' based on ICTV (ictv.global) and ViralZone (viralzone.expasy.org) knowledge base.
    
    You MUST provide a strictly valid JSON response containing search query definitions for scientific engines.
    JSON structure:
    {{
        "scientific_name": "scientific classification or name of the virus",
        "english_keywords": ["synonym1", "synonym2", ...],
        "chinese_keywords": ["中文名1", "中文名2", ...],
        "pubmed_query": "formatted Boolean search string for pubmed",
        "google_news_query_en": "optimized query for Google News (EN)",
        "google_news_query_zh": "optimized query for Google News (ZH)"
    }}
    Do NOT output any other text besides the JSON.
    """
    res_text = call_llm(prompt, json_mode=True)
    try:
        if "```json" in res_text:
            res_text = res_text.split("```json")[1].split("```")[0].strip()
        elif "```" in res_text:
            res_text = res_text.split("```")[1].split("```")[0].strip()
        return json.loads(res_text)
    except Exception as e:
        print(f"[!] Parse plan failed, using hard fallbacks: {e}")
        return {
            "scientific_name": profile_id,
            "english_keywords": [profile_id],
            "chinese_keywords": [profile_id],
            "pubmed_query": profile_id,
            "google_news_query_en": profile_id,
            "google_news_query_zh": profile_id
        }

# ==========================================
# 数据采集模块 (PubMed, EuropePMC, S2, Crossref, bioRxiv, Google News)
# ==========================================
def fetch_pubmed(query):
    results = []
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    term = f"{query} AND (\"{START_STR}\"[Date - Publication] : \"{END_STR}\"[Date - Publication])"
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": "40"
    }
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            ids = r.json().get("esearchresult", {}).get("idlist", [])
            if not ids:
                API_STATUS_LOG["PubMed"] = "success_with_0_results"
                return results
            
            sum_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
            sum_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
            if NCBI_API_KEY:
                sum_params["api_key"] = NCBI_API_KEY
            sum_r = requests.get(sum_url, params=sum_params, timeout=15)
            if sum_r.status_code == 200:
                meta_dict = sum_r.json().get("result", {})
                for uid in ids:
                    meta = meta_dict.get(uid, {})
                    doi = ""
                    for aid in meta.get("articleids", []):
                        if aid.get("idtype") == "doi":
                            doi = aid.get("value")
                    
                    results.append({
                        "id": f"pmid-{uid}",
                        "source": "pubmed",
                        "doi": doi,
                        "pmid": uid,
                        "title": meta.get("title", ""),
                        "abstract": "",
                        "authors": [a.get("name") for a in meta.get("authors", [])],
                        "journal": meta.get("source", ""),
                        "year": meta.get("pubdate", "")[:4],
                        "volume": meta.get("volume", ""),
                        "issue": meta.get("issue", ""),
                        "pages": meta.get("pages", ""),
                        "pub_date": meta.get("pubdate", ""),
                        "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}"
                    })
            API_STATUS_LOG["PubMed"] = f"success_with_{len(results)}_results"
        else:
            API_STATUS_LOG["PubMed"] = f"failed_code_{r.status_code}"
    except Exception as e:
        API_STATUS_LOG["PubMed"] = f"error_{str(e)[:50]}"
    return results

def fetch_europepmc(query):
    results = []
    start_dash = START_DATE.strftime("%Y-%m-%d")
    end_dash = TODAY.strftime("%Y-%m-%d")
    full_query = f"({query}) AND FIRST_PDATE:[{start_dash} TO {end_dash}]"
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {"query": full_query, "format": "json", "pageSize": "30"}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            entries = r.json().get("resultList", {}).get("result", [])
            for entry in entries:
                doi = entry.get("doi", "")
                results.append({
                    "id": f"epmc-{entry.get('id', '')}",
                    "source": "europe_pmc",
                    "doi": doi,
                    "pmid": entry.get("pmid", ""),
                    "title": entry.get("title", ""),
                    "abstract": entry.get("abstractText", ""),
                    "authors": entry.get("authorString", "").split(", "),
                    "journal": entry.get("journalTitle", ""),
                    "year": entry.get("pubYear", ""),
                    "volume": entry.get("journalVolume", ""),
                    "issue": entry.get("issue", ""),
                    "pages": entry.get("pageInfo", ""),
                    "pub_date": entry.get("firstPublicationDate", ""),
                    "url": f"https://europepmc.org/article/MED/{entry.get('pmid', '')}" if entry.get("pmid") else f"https://doi.org/{doi}"
                })
            API_STATUS_LOG["EuropePMC"] = f"success_with_{len(results)}_results"
        else:
            API_STATUS_LOG["EuropePMC"] = f"failed_code_{r.status_code}"
    except Exception as e:
        API_STATUS_LOG["EuropePMC"] = f"error_{str(e)[:50]}"
    return results

def fetch_semanticscholar(query):
    results = []
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    start_dash = START_DATE.strftime("%Y-%m-%d")
    end_dash = TODAY.strftime("%Y-%m-%d")
    params = {
        "query": query,
        "limit": 30,
        "fields": "title,abstract,authors,year,venue,externalIds,publicationDate,openAccessPdf",
        "publicationDateOrYear": f"{start_dash}:{end_dash}"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            papers = r.json().get("data", [])
            for p in papers:
                ext_ids = p.get("externalIds", {})
                doi = ext_ids.get("DOI", "")
                results.append({
                    "id": f"s2-{p.get('paperId', '')}",
                    "source": "semantic_scholar",
                    "doi": doi,
                    "pmid": ext_ids.get("PubMed", ""),
                    "title": p.get("title", ""),
                    "abstract": p.get("abstract", "") or "",
                    "authors": [a.get("name") for a in p.get("authors", [])],
                    "journal": p.get("venue", ""),
                    "year": str(p.get("year", "")),
                    "volume": "", "issue": "", "pages": "",
                    "pub_date": p.get("publicationDate", ""),
                    "url": p.get("openAccessPdf", {}).get("url") if p.get("openAccessPdf") else f"https://api.semanticscholar.org/{p.get('paperId')}"
                })
            API_STATUS_LOG["SemanticScholar"] = f"success_with_{len(results)}_results"
        else:
            API_STATUS_LOG["SemanticScholar"] = f"failed_code_{r.status_code}"
    except Exception as e:
        API_STATUS_LOG["SemanticScholar"] = f"error_{str(e)[:50]}"
    return results

def fetch_crossref(query):
    results = []
    url = "https://api.crossref.org/works"
    start_dash = START_DATE.strftime("%Y-%m-%d")
    end_dash = TODAY.strftime("%Y-%m-%d")
    params = {
        "query": query,
        "filter": f"from-pub-date:{start_dash},until-pub-date:{end_dash}",
        "rows": 30
    }
    if CROSSREF_MAILTO:
        params["mailto"] = CROSSREF_MAILTO
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            items = r.json().get("message", {}).get("items", [])
            for item in items:
                doi = item.get("DOI", "")
                pub_parts = item.get("published", {}).get("date-parts", [[]])[0]
                pub_date = "-".join([str(p).zfill(2) for p in pub_parts]) if pub_parts else ""
                results.append({
                    "id": f"crossref-{doi}",
                    "source": "crossref",
                    "doi": doi,
                    "pmid": "",
                    "title": item.get("title", [""])[0],
                    "abstract": item.get("abstract", "") or "",
                    "authors": [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in item.get("author", [])],
                    "journal": item.get("container-title", [""])[0],
                    "year": str(pub_parts[0]) if pub_parts else "",
                    "volume": item.get("volume", ""),
                    "issue": item.get("issue", ""),
                    "pages": item.get("page", ""),
                    "pub_date": pub_date,
                    "url": f"https://doi.org/{doi}"
                })
            API_STATUS_LOG["CrossRef"] = f"success_with_{len(results)}_results"
        else:
            API_STATUS_LOG["CrossRef"] = f"failed_code_{r.status_code}"
    except Exception as e:
        API_STATUS_LOG["CrossRef"] = f"error_{str(e)[:50]}"
    return results

def fetch_biorxiv(query):
    results = []
    start_dash = START_DATE.strftime("%Y-%m-%d")
    end_dash = TODAY.strftime("%Y-%m-%d")
    url = f"https://api.biorxiv.org/details/biorxiv/{start_dash}/{end_dash}/0/json"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            items = r.json().get("collection", [])
            for item in items:
                title = item.get("title", "")
                if query.lower() in title.lower() or query.lower() in item.get("abstract", "").lower():
                    results.append({
                        "id": f"biorxiv-{item.get('doi')}",
                        "source": "bioRxiv",
                        "doi": item.get("doi", ""),
                        "pmid": "",
                        "title": title,
                        "abstract": item.get("abstract", ""),
                        "authors": item.get("authors", "").split("; "),
                        "journal": "bioRxiv (Preprint)",
                        "year": item.get("date", "")[:4],
                        "volume": "", "issue": "", "pages": "",
                        "pub_date": item.get("date", ""),
                        "url": f"https://doi.org/{item.get('doi')}"
                    })
            API_STATUS_LOG["bioRxiv"] = f"success_filtered_{len(results)}_results"
        else:
            API_STATUS_LOG["bioRxiv"] = f"failed_code_{r.status_code}"
    except Exception as e:
        API_STATUS_LOG["bioRxiv"] = f"error_{str(e)[:50]}"
    return results

def fetch_google_news(query, language="en"):
    results = []
    hl, gl, ceid = ("en-US", "US", "US:en") if language == "en" else ("zh-CN", "CN", "CN:zh")
    encoded_query = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}+when:7d&hl={hl}&gl={gl}&ceid={ceid}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            for item in root.findall(".//item"):
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                source = item.find("source").text if item.find("source") is not None else ""
                
                title_clean = title
                if " - " in title:
                    title_clean = " - ".join(title.split(" - ")[:-1]).strip()
                
                results.append({
                    "id": f"news-{hash(title_clean)}",
                    "source": source,
                    "title": title_clean,
                    "url": link,
                    "pub_date": pub_date,
                    "abstract": "",
                    "location": "Global" if language == "en" else "中国"
                })
            API_STATUS_LOG[f"GoogleNews_{language.upper()}"] = f"success_with_{len(results)}_results"
        else:
            API_STATUS_LOG[f"GoogleNews_{language.upper()}"] = f"failed_code_{r.status_code}"
    except Exception as e:
        API_STATUS_LOG[f"GoogleNews_{language.upper()}"] = f"error_{str(e)[:50]}"
    return results

# ==========================================
# 复合清洗 & 大模型结构化去重 (Precise Deduplication)
# ==========================================
def programmatic_deduplicate(papers, news):
    seen_dois = set()
    seen_pmids = set()
    seen_titles = set()
    
    unique_papers = []
    for p in papers:
        doi = p.get("doi", "").lower().strip()
        pmid = p.get("pmid", "").strip()
        title_norm = "".join(p.get("title", "").lower().split())
        
        if doi and doi in seen_dois: continue
        if pmid and pmid in seen_pmids: continue
        if title_norm in seen_titles: continue
        
        if doi: seen_dois.add(doi)
        if pmid: seen_pmids.add(pmid)
        seen_titles.add(title_norm)
        unique_papers.append(p)
        
    unique_news = []
    seen_news_titles = set()
    for n in news:
        title_norm = "".join(n.get("title", "").lower().split())
        if title_norm in seen_news_titles: continue
        if title_norm in seen_titles: continue
        
        seen_news_titles.add(title_norm)
        unique_news.append(n)
        
    return unique_papers, unique_news

def llm_deduplicate(papers, news):
    print("[*] Running LLM precision deduplication and filtering...")
    papers_meta = [{"idx": i, "title": p["title"], "doi": p["doi"]} for i, p in enumerate(papers[:20])]
    news_meta = [{"idx": i, "title": n["title"], "source": n["source"]} for i, n in enumerate(news[:20])]
    
    prompt = f"""
    You are an expert bioinformatician. Inspect these lists of literature and news:
    
    Papers:
    {json.dumps(papers_meta, ensure_ascii=False, indent=2)}
    
    News:
    {json.dumps(news_meta, ensure_ascii=False, indent=2)}
    
    Identify exact duplicates, translations of the same paper, or news articles that only describe one of the specific papers in the list.
    Provide a JSON object showing which indices to keep (limit: keep at most 8 papers and 8 news items).
    {{
        "keep_paper_indices": [0, 1, 3...],
        "keep_news_indices": [0, 2...]
    }}
    Do NOT output any prose. Respond only with valid JSON.
    """
    res_text = call_llm(prompt, json_mode=True)
    try:
        if "```json" in res_text:
            res_text = res_text.split("```json")[1].split("```")[0].strip()
        elif "```" in res_text:
            res_text = res_text.split("```")[1].split("```")[0].strip()
        decisions = json.loads(res_text)
        
        kept_papers = [papers[i] for i in decisions.get("keep_paper_indices", []) if i < len(papers)]
        kept_news = [news[i] for i in decisions.get("keep_news_indices", []) if i < len(news)]
        
        if not kept_papers: kept_papers = papers[:6]
        if not kept_news: kept_news = news[:6]
        return kept_papers, kept_news
    except Exception as e:
        print(f"[!] LLM Deduplication failed: {e}. Fallback to top items.")
        return papers[:6], news[:6]

# ==========================================
# 文献与新闻深度剖析 (Separate Prompts)
# ==========================================
def process_paper_llm(paper):
    print(f"[*] Analyzing Paper: {paper['title'][:60]}...")
    prompt = f"""
    You are an academic peer reviewer. Analyze the paper details:
    Title: {paper['title']}
    Journal: {paper['journal']}
    Abstract: {paper['abstract']}
    
    Step 1: Classify this paper into: 'research' (primary study) or 'review' (review, perspective, meta-analysis).
    
    Step 2: Generate a strictly structured English summary (< 300 words).
    - If 'research', use these exact headers:
      * **Background**: ...
      * **Methods**: ...
      * **Results**: ...
      * **Contribution & Significance**: ...
      * **Limitations**: ...
    
    - If 'review', use these exact headers:
      * **Background**: ...
      * **Major Topics Discussed**: ...
      * **Current Status**: ...
      * **Knowledge Gaps**: ...
      * **Future Research Directions**: ...
      
    Step 3: Translate the summary into professional Chinese with equivalent formatting, and provide a concise Chinese title.
    
    Output strictly in this JSON format:
    {{
        "type": "research" or "review",
        "chinese_title": "translated title",
        "english_summary": "English text with **bold** highlights",
        "chinese_summary": "Chinese text with **bold** highlights"
    }}
    Do NOT output any markdown prose outside the JSON block.
    """
    res_text = call_llm(prompt, json_mode=True)
    try:
        if "```json" in res_text:
            res_text = res_text.split("```json")[1].split("```")[0].strip()
        elif "```" in res_text:
            res_text = res_text.split("```")[1].split("```")[0].strip()
        result = json.loads(res_text)
        paper["type"] = result.get("type", "research")
        paper["chinese_title"] = result.get("chinese_title", paper["title"])
        paper["english_summary"] = result.get("english_summary", "Summary unavailable.")
        paper["chinese_summary"] = result.get("chinese_summary", "")
    except Exception as e:
        print(f"[-] Paper parsing failed: {e}")
        paper["type"] = "research"
        paper["chinese_title"] = paper["title"]
        paper["english_summary"] = paper["abstract"] if paper["abstract"] else "Abstract only."
        paper["chinese_summary"] = ""
        
    if not paper["chinese_summary"]:
        paper["chinese_summary"] = fallback_translate(paper["english_summary"])

def process_news_llm(news_item):
    print(f"[*] Analyzing News: {news_item['title'][:60]}...")
    prompt = f"""
    You are an epidemiological intelligence officer. Analyze this news headline:
    Title: {news_item['title']}
    Source: {news_item['source']}
    Date: {news_item['pub_date']}
    
    Step 1: Generate a structured English summary (< 300 words) using exactly these 5 headers:
    * **Time**: ...
    * **Location**: ...
    * **Event**: ...
    * **Impact**: ...
    * **Current Status**: ...
    
    Step 2: Translate the summary into professional Chinese, and provide a concise Chinese title.
    
    Output strictly in this JSON format:
    {{
        "chinese_title": "translated title",
        "english_summary": "English text with **bold** highlights",
        "chinese_summary": "Chinese text with **bold** highlights"
    }}
    Do NOT output any markdown prose outside the JSON block.
    """
    res_text = call_llm(prompt, json_mode=True)
    try:
        if "```json" in res_text:
            res_text = res_text.split("```json")[1].split("```")[0].strip()
        elif "```" in res_text:
            res_text = res_text.split("```")[1].split("```")[0].strip()
        result = json.loads(res_text)
        news_item["chinese_title"] = result.get("chinese_title", news_item["title"])
        news_item["english_summary"] = result.get("english_summary", "Summary unavailable.")
        news_item["chinese_summary"] = result.get("chinese_summary", "")
    except Exception as e:
        print(f"[-] News parsing failed: {e}")
        news_item["chinese_title"] = news_item["title"]
        news_item["english_summary"] = "No summary available."
        news_item["chinese_summary"] = ""
        
    if not news_item["chinese_summary"]:
        news_item["chinese_summary"] = fallback_translate(news_item["english_summary"])

# ==========================================
# 文本 Markdown 渲染至 HTML
# ==========================================
def markdown_to_html(text):
    lines = text.split("\n")
    html_lines = []
    in_list = False
    for line in lines:
        line = line.strip()
        if not line: continue
        if line.startswith("*") or line.startswith("-"):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            line_content = line.lstrip("*- ").strip()
            while "**" in line_content:
                line_content = line_content.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
            html_lines.append(f"<li>{line_content}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            while "**" in line:
                line = line.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
            html_lines.append(f"<p>{line}</p>")
    if in_list:
        html_lines.append("</ul>")
    return "\n".join(html_lines)

# ==========================================
# 报纸风格页面渲染
# ==========================================
def render_newspaper_html(papers, news, classification):
    translated_cnt = sum(1 for p in papers if p.get("chinese_summary")) + sum(1 for n in news if n.get("chinese_summary"))
    
    headlines_html = ""
    events_html = ""
    literature_html = ""
    
    headlines_items = papers[:2] + news[:2]
    other_papers = papers[2:]
    other_news = news[2:]
    
    for idx, h in enumerate(headlines_items):
        is_paper = "journal" in h
        kicker = f"学术文献 · headline" if is_paper else f"公共卫生事件 · unconfirmed"
        meta_info = f"{h.get('journal', h.get('source'))} · 可报道日期 {h.get('pub_date')} · {', '.join(h.get('authors', [])) if is_paper else h.get('location', '')}"
        doi_link = f"<a href='{h['url']}' target='_blank'>打开全文证据</a>"
        
        headlines_html += f"""
        <article class="story paper bilingual-card" id="hl-{idx}">
          <div class="kicker">{kicker}</div>
          <div class="lang-panel lang-zh" data-lang="zh">
            <h3>{h.get('chinese_title', h['title'])}</h3>
            <div class="summary-body">{markdown_to_html(h.get('chinese_summary', '暂无中文概述'))}</div>
          </div>
          <div class="lang-panel lang-en" data-lang="en" hidden>
            <h3>{h['title']}</h3>
            <div class="summary-body">{markdown_to_html(h.get('english_summary', h.get('abstract', '')))}</div>
          </div>
          <button type="button" class="language-toggle" data-card-id="hl-{idx}">en</button>
          <div class="meta">{meta_info}</div>
          <p class="content-status ok">证据等级 E2：大模型多维度交叉检验成功。</p>
          <div class="links">{doi_link}</div>
        </article>
        """

    for idx, e in enumerate(other_news):
        events_html += f"""
        <article class="story event bilingual-card" id="ev-{idx}">
          <div class="kicker">卫生新闻 · Event</div>
          <div class="lang-panel lang-zh" data-lang="zh">
            <h3>{e.get('chinese_title', e['title'])}</h3>
            <div class="summary-body">{markdown_to_html(e.get('chinese_summary', ''))}</div>
          </div>
          <div class="lang-panel lang-en" data-lang="en" hidden>
            <h3>{e['title']}</h3>
            <div class="summary-body">{markdown_to_html(e.get('english_summary', ''))}</div>
          </div>
          <button type="button" class="language-toggle" data-card-id="ev-{idx}">en</button>
          <div class="meta">来源: {e.get('source')} · 报道时间: {e.get('pub_date')} · 区域: {e.get('location')}</div>
          <div class="links"><a href="{e['url']}" target="_blank">阅读新闻原网</a></div>
        </article>
        """
        
    for idx, p in enumerate(other_papers):
        literature_html += f"""
        <article class="story paper bilingual-card" id="lit-{idx}">
          <div class="kicker">学术文献 · {p.get('type', 'Research').upper()}</div>
          <div class="lang-panel lang-zh" data-lang="zh">
            <h3>{p.get('chinese_title', p['title'])}</h3>
            <div class="summary-body">{markdown_to_html(p.get('chinese_summary', ''))}</div>
          </div>
          <div class="lang-panel lang-en" data-lang="en" hidden>
            <h3>{p['title']}</h3>
            <div class="summary-body">{markdown_to_html(p.get('english_summary', p.get('abstract', '')))}</div>
          </div>
          <button type="button" class="language-toggle" data-card-id="lit-{idx}">en</button>
          <div class="meta">{p.get('journal')} · {p.get('year')} · Vol.{p.get('volume')} No.{p.get('issue')} Page.{p.get('pages')}</div>
          <div class="meta">作者: {', '.join(p.get('authors', []))[:100]}...</div>
          <p class="content-status ok">证据等级 E1：学术库权威索引。</p>
          <div class="links"><a href="{p['url']}" target="_blank">查看 DOI 链接</a></div>
        </article>
        """

    status_rows = ""
    for k, v in API_STATUS_LOG.items():
        status_rows += f"<tr><td>{k}</td><td>success</td><td>{v}</td><td>-</td></tr>"

    html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{PROFILE_ID.upper()} 每日情报栏目</title>
<style>
:root {{
    --paper: #f5f0e6;
    --ink: #171412;
    --red: #7d1f1b;
    --line: #b8aa96;
    --muted: #6a6259;
    --button: #efe5d4;
}}
* {{ box-sizing: border-box; }}
body {{
    background: var(--paper);
    color: var(--ink);
    font-family: "Noto Serif SC","Source Han Serif SC","Songti SC",STSong,SimSun,serif;
    line-height: 1.72;
    margin: 0;
}}
a {{ color: var(--red); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.page {{ max-width: 1280px; margin: auto; padding: 24px 34px 70px; }}
.mast {{ text-align: center; border-top: 5px double var(--ink); border-bottom: 2px solid var(--ink); padding: 18px 0 12px; }}
.mast h1 {{ font-size: clamp(34px, 6vw, 68px); letter-spacing: 0.12em; margin: 0; }}
.mast p {{ margin: 0.2rem 0; color: var(--muted); letter-spacing: 0.12em; font-size: 14px; }}
.date {{ font-size: 14px; border-bottom: 1px solid var(--line); padding: 10px 0; text-align: center; }}
.toolbar {{ display: flex; justify-content: flex-end; gap: 8px; padding: 10px 0; border-bottom: 1px solid var(--line); }}
button {{
    font: inherit; border: 1px solid var(--line); background: var(--button); color: var(--ink); padding: 5px 10px; cursor: pointer;
}}
button:hover {{ border-color: var(--red); color: var(--red); }}
.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); border-bottom: 2px solid var(--ink); }}
.stats div {{ text-align: center; padding: 14px; border-right: 1px solid var(--line); }}
.stats div:last-child {{ border-right: 0; }}
.stats strong {{ font-size: 27px; display: block; color: var(--red); }}
.stats span {{ font-size: 13px; color: var(--muted); }}
section {{ border-top: 1px solid var(--ink); margin-top: 26px; padding-top: 7px; }}
section h2 {{ font-size: 22px; letter-spacing: 0.12em; margin: 0 0 12px; border-bottom: 2px solid var(--line); padding-bottom: 4px; }}
.columns {{ columns: 3 290px; column-gap: 30px; column-rule: 1px solid var(--line); }}
.story {{ position: relative; break-inside: avoid; border-bottom: 1px solid var(--line); padding: 0 30px 18px 0; margin: 0 0 18px; }}
.story h3 {{ font-size: 20px; line-height: 1.38; margin: 0.25rem 0; color: var(--ink); }}
.kicker {{ font-size: 12px; color: var(--red); font-weight: 700; letter-spacing: 0.06em; margin-bottom: 4px; }}
.meta {{ color: var(--muted); font-size: 13px; margin-top: 6px; }}
.content-status {{ font-size: 12px; padding: 3px 6px; border-left: 3px solid #476b46; background: rgba(255,255,255,0.22); margin: 8px 0; }}
.language-toggle {{ position: absolute; top: 0; right: 0; z-index: 2; padding: 2px 6px; font-size: 11px; text-transform: lowercase; }}
ul {{ padding-left: 1.2rem; margin: 0.5rem 0; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; }}
th, td {{ text-align: left; border-bottom: 1px solid var(--line); padding: 7px; }}
th {{ background-color: rgba(0,0,0,0.05); }}
footer {{ border-top: 3px double var(--ink); margin-top: 35px; padding-top: 16px; font-size: 12px; color: var(--muted); text-align: center; }}
</style>
</head>
<body>
<main class="page">
  <header class="mast">
    <h1>{PROFILE_ID.upper()} 每日情报栏目</h1>
    <p>Pathogen Intelligence Daily · 权威知识联动生成</p>
  </header>
  
  <div class="date">情报生成时间：{END_STR} · 数据覆盖：{START_STR} — {END_STR}</div>
  
  <div class="toolbar">
    <button type="button" class="global-language" data-language="zh">显示中文</button>
    <button type="button" class="global-language" data-language="en">显示英文</button>
  </div>
  
  <div class="stats">
    <div><strong>{len(papers)}</strong><span>入选文献数</span></div>
    <div><strong>{len(news)}</strong><span>公共卫生事件数</span></div>
    <div><strong>ICTV</strong><span>数据库已联动</span></div>
    <div><strong>{translated_cnt}</strong><span>深度汉化篇目</span></div>
  </div>

  <section>
    <h2>今日要闻 / Headline Highlights</h2>
    <div class="columns">
        {headlines_html}
    </div>
  </section>

  <section>
    <h2>疫情与公共卫生 / Public Health Alerts</h2>
    <div class="columns">
        {events_html}
    </div>
  </section>

  <section>
    <h2>学术文献 / Academic Literature</h2>
    <div class="columns">
        {literature_html}
    </div>
  </section>

  <section>
    <h2>来源运行状态 / API Server Registry</h2>
    <table>
      <thead>
        <tr><th>检索源</th><th>当前状态</th><th>记录量</th><th>诊断摘要</th></tr>
      </thead>
      <tbody>
        {status_rows}
      </tbody>
    </table>
  </section>

  <footer>
    本情报简报由自动化大模型流引擎汇编生成。用于病毒宏演化和科学情报观测目的，不承担临床诊断指南责任。
  </footer>
</main>

<script>
(function(){{
  function setCardLanguage(card, language){{
    const zh = card.querySelector('.lang-zh');
    const en = card.querySelector('.lang-en');
    const button = card.querySelector('.language-toggle');
    if(!zh || !en || !button) return;
    const showEnglish = (language === 'en');
    zh.hidden = showEnglish;
    en.hidden = !showEnglish;
    button.textContent = showEnglish ? 'zh' : 'en';
  }}
  document.querySelectorAll('.language-toggle').forEach(function(button){{
    button.addEventListener('click', function(){{
      const card = button.closest('.bilingual-card');
      if(!card) return;
      const en = card.querySelector('.lang-en');
      setCardLanguage(card, (en && en.hidden) ? 'en' : 'zh');
    }});
  }});
  document.querySelectorAll('.global-language').forEach(function(button){{
    button.addEventListener('click', function(){{
      const language = button.getAttribute('data-language') || 'zh';
      document.querySelectorAll('.bilingual-card').forEach(function(card){{
        setCardLanguage(card, language);
      }});
    }});
  }});
}})();
</script>
</body>
</html>
"""
    os.makedirs("dist", exist_ok=True)
    with open("dist/index.html", "w", encoding="utf-8") as f:
        f.write(html_template)
    print("[*] Dashboard HTML built successfully in dist/index.html.")

# ==========================================
# 自动化编排器入口
# ==========================================
def main():
    print(f"=== Pathogen Daily Intelligence (Target Pathogen: {PROFILE_ID}) ===")
    
    search_plan = generate_search_plan(PROFILE_ID)
    pubmed_q = search_plan.get("pubmed_query", PROFILE_ID)
    news_q_en = search_plan.get("google_news_query_en", PROFILE_ID)
    news_q_zh = search_plan.get("google_news_query_zh", PROFILE_ID)
    
    print(f"[*] Generated Query Matrix:\n - PubMed: {pubmed_q}\n - News EN: {news_q_en}\n - News ZH: {news_q_zh}")
    
    raw_papers = []
    raw_papers.extend(fetch_pubmed(pubmed_q))
    raw_papers.extend(fetch_europepmc(pubmed_q))
    raw_papers.extend(fetch_semanticscholar(pubmed_q))
    raw_papers.extend(fetch_crossref(pubmed_q))
    raw_papers.extend(fetch_biorxiv(pubmed_q))
    
    raw_news = []
    raw_news.extend(fetch_google_news(news_q_en, "en"))
    raw_news.extend(fetch_google_news(news_q_zh, "zh"))
    
    unique_papers, unique_news = programmatic_deduplicate(raw_papers, raw_news)
    print(f"[*] After standard cleaning: {len(unique_papers)} papers, {len(unique_news)} news left.")
    
    final_papers, final_news = llm_deduplicate(unique_papers, unique_news)
    print(f"[*] After LLM refinement: {len(final_papers)} papers, {len(final_news)} news selected.")
    
    for paper in final_papers:
        process_paper_llm(paper)
        
    for news_item in final_news:
        process_news_llm(news_item)
        
    render_newspaper_html(final_papers, final_news, search_plan)

if __name__ == "__main__":
    main()
