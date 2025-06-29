from flask import Flask, request, Response
import requests
from urllib.parse import urlparse, urljoin, quote, unquote
import re
import json
import os
import random
from dotenv import load_dotenv
from cachetools import TTLCache, LRUCache

load_dotenv()  # Carica le variabili dal file .env

app = Flask(__name__)

# --- Configurazione Cache ---
# Cache per le playlist M3U8. TTL di 5 secondi per garantire l'aggiornamento dei live stream.
M3U8_CACHE = TTLCache(maxsize=200, ttl=5)
# Cache per i segmenti TS. LRU (Least Recently Used) per mantenere i segmenti più richiesti.
TS_CACHE = LRUCache(maxsize=1000)  # Mantiene in memoria i 1000 segmenti usati più di recente
# Cache per le chiavi di decriptazione.
KEY_CACHE = LRUCache(maxsize=200)
# --- Configurazione Proxy ---
# I proxy possono essere in formato http, https, socks5, socks5h. Es: 'socks5://user:pass@host:port'
# È possibile specificare una lista di proxy separati da virgola. Verrà scelto uno a caso.
NEWKSO_PROXY = os.getenv('NEWKSO_PROXY', None)
NEWKSO_SSL_VERIFY = os.getenv('NEWKSO_SSL_VERIFY', 'false').lower() == 'true'

VAVOO_PROXY = os.getenv('VAVOO_PROXY', None)
VAVOO_SSL_VERIFY = os.getenv('VAVOO_SSL_VERIFY', 'false').lower() == 'true'

GENERAL_PROXY = os.getenv('GENERAL_PROXY', None)
GENERAL_SSL_VERIFY = os.getenv('GENERAL_SSL_VERIFY', 'false').lower() == 'true'

# Disabilita gli avvisi di richiesta non sicura se la verifica SSL è disattivata per QUALSIASI proxy
if not all([NEWKSO_SSL_VERIFY, VAVOO_SSL_VERIFY, GENERAL_SSL_VERIFY]):
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DADDY_PHP_DOMAINS_MATCH = [
    "new.newkso.ru/wind/",
    "new.newkso.ru/ddy6/",
    "new.newkso.ru/zeko/",
    "new.newkso.ru/nfs/",
    "new.newkso.ru/dokko1/",
]

DADDY_PHP_SITES_URLS = [
    "https://new.newkso.ru/wind/",
    "https://new.newkso.ru/ddy6/",
    "https://new.newkso.ru/zeko/",
    "https://new.newkso.ru/nfs/",
    "https://new.newkso.ru/dokko1/",
]

def _get_proxy_dict(proxy_env_var):
    """Helper per creare un dizionario di proxy da una variabile d'ambiente."""
    if not proxy_env_var:
        return None
    proxy_list = [p.strip() for p in proxy_env_var.split(',')]
    selected_proxy = random.choice(proxy_list)
    # la libreria requests gestisce lo schema (http, https, socks5, socks5h) dall'URL del proxy
    return {'http': selected_proxy, 'https': selected_proxy}

def get_proxy_config_for_url(url):
    """
    Restituisce la configurazione del proxy (dizionario proxy e flag di verifica SSL) per un dato URL.
    Supporta proxy specifici per dominio (newkso, vavoo) e un proxy generale.
    """
    lower_url = url.lower()
    parsed_url = urlparse(lower_url)

    # 1. newkso.ru e daddy_php_sites
    is_newkso = "newkso.ru" in parsed_url.netloc or any(domain in lower_url for domain in DADDY_PHP_DOMAINS_MATCH)
    if is_newkso and NEWKSO_PROXY:
        return {"proxies": _get_proxy_dict(NEWKSO_PROXY), "verify": NEWKSO_SSL_VERIFY}

    # 2. vavoo.to
    if "vavoo.to" in parsed_url.netloc and VAVOO_PROXY:
        return {"proxies": _get_proxy_dict(VAVOO_PROXY), "verify": VAVOO_SSL_VERIFY}

    # 3. Proxy Generale (se non corrisponde a domini specifici)
    if GENERAL_PROXY:
        return {"proxies": _get_proxy_dict(GENERAL_PROXY), "verify": GENERAL_SSL_VERIFY}

    # 4. Nessun proxy
    return {"proxies": None, "verify": True}

def detect_m3u_type(content):
    """Rileva se è un M3U (lista IPTV) o un M3U8 (flusso HLS)"""
    if "#EXTM3U" in content and "#EXTINF" in content:
        return "m3u8"
    return "m3u"

def replace_key_uri(line, headers_query):
    """Sostituisce l'URI della chiave AES-128 con il proxy"""
    match = re.search(r'URI="([^"]+)"', line)
    if match:
        key_url = match.group(1)
        proxied_key_url = f"/proxy/key?url={quote(key_url)}&{headers_query}"
        return line.replace(key_url, proxied_key_url)
    return line

def resolve_m3u8_link(url, headers=None):
    """
    Risolve un URL M3U8 supportando header e proxy per newkso.ru e daddy_php_sites.
    """
    if not url:
        app.logger.error("URL non fornito.")
        return {"resolved_url": None, "headers": {}}

    app.logger.info(f"Tentativo di risoluzione URL: {url}")

    # Inizializza gli header di default
    current_headers = headers if headers else {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36'
    }
    
    clean_url = url
    extracted_headers = {}

    # Estrazione header da URL
    if '&h_' in url or '%26h_' in url:
        app.logger.info("Rilevati parametri header nell'URL - Estrazione in corso...")
        if '%26h_' in url:
            if 'vavoo.to' in url.lower():
                url = url.replace('%26', '&')
            else:
                url = unquote(unquote(url))
        url_parts = url.split('&h_', 1)
        clean_url = url_parts[0]
        header_params = '&h_' + url_parts[1]
        for param in header_params.split('&'):
            if param.startswith('h_'):
                try:
                    key_value = param[2:].split('=', 1)
                    if len(key_value) == 2:
                        key = unquote(key_value[0]).replace('_', '-')
                        value = unquote(key_value[1])
                        extracted_headers[key] = value
                except Exception as e:
                    app.logger.error(f"Errore nell'estrazione dell'header {param}: {e}")
        current_headers.update(extracted_headers)
    else:
        app.logger.info("URL pulito rilevato - Nessuna estrazione header necessaria")

    # Gestione .php Daddy
    if clean_url.endswith('.php'):
        app.logger.info(f"Rilevato URL .php {clean_url}")
        channel_id_match = re.search(r'stream-(\d+)\.php', clean_url)
        if channel_id_match:
            channel_id = channel_id_match.group(1)
            app.logger.info(f"Channel ID estratto: {channel_id}")

            newkso_headers_for_php_resolution = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
                'Referer': 'https://forcedtoplay.xyz/',
                'Origin': 'https://forcedtoplay.xyz/'
            }

            # Tennis Channels
            if channel_id.startswith("15") and len(channel_id) == 4:
                tennis_suffix = channel_id[2:]
                folder_name = f"wikiten{tennis_suffix}"
                test_url = f"https://new.newkso.ru/wikihz/{folder_name}/mono.m3u8"
                app.logger.info(f"Tentativo canale Tennis: {test_url}")
                try:
                    proxy_config = get_proxy_config_for_url(test_url)
                    response = requests.head(test_url, headers=newkso_headers_for_php_resolution, 
                                           proxies=proxy_config['proxies'], timeout=5, allow_redirects=True,
                                           verify=proxy_config['verify'])
                    if response.status_code == 200:
                        app.logger.info(f"Stream Tennis trovato: {test_url}")
                        return {"resolved_url": test_url, "headers": newkso_headers_for_php_resolution}
                except requests.RequestException as e:
                    app.logger.warning(f"Errore HEAD per Tennis stream {test_url}: {e}")
            
            # Daddy Channels
            else:
                folder_name = f"premium{channel_id}"
                for site in DADDY_PHP_SITES_URLS:
                    test_url = f"{site}{folder_name}/mono.m3u8"
                    app.logger.info(f"Tentativo canale Daddy: {test_url}")
                    try:
                        proxy_config = get_proxy_config_for_url(test_url)
                        response = requests.head(test_url, headers=newkso_headers_for_php_resolution, 
                                               proxies=proxy_config['proxies'], timeout=5, allow_redirects=True,
                                               verify=proxy_config['verify'])
                        if response.status_code == 200:
                            app.logger.info(f"Stream Daddy trovato: {test_url}")
                            return {"resolved_url": test_url, "headers": newkso_headers_for_php_resolution}
                    except requests.RequestException as e:
                        app.logger.warning(f"Errore HEAD per Daddy stream {test_url}: {e}")

    # Fallback: richiesta normale
    try:
        with requests.Session() as session:
            proxy_config = get_proxy_config_for_url(clean_url)
            if proxy_config['proxies']:
                app.logger.debug(f"Proxy in uso per {clean_url}")
            app.logger.info(f"Passo 1: Richiesta a {clean_url}")
            response = session.get(clean_url, headers=current_headers, proxies=proxy_config['proxies'], 
                                 allow_redirects=True, timeout=(10, 20), verify=proxy_config['verify'])
            response.raise_for_status()
            initial_response_text = response.text
            final_url_after_redirects = response.url
            app.logger.info(f"Passo 1 completato. URL finale dopo redirect: {final_url_after_redirects}")

            if initial_response_text and initial_response_text.strip().startswith('#EXTM3U'):
                app.logger.info("Trovato file M3U8 diretto.")
                return {
                    "resolved_url": final_url_after_redirects,
                    "headers": current_headers
                }
            else:
                app.logger.info("La risposta iniziale non era un M3U8 diretto.")
                return {
                    "resolved_url": clean_url,
                    "headers": current_headers
                }

    except requests.RequestException as e:
        app.logger.error(f"Errore durante la richiesta HTTP iniziale: {e}")
        return {"resolved_url": clean_url, "headers": current_headers}
    except Exception as e:
        app.logger.error(f"Errore generico durante la risoluzione: {e}")
        return {"resolved_url": clean_url, "headers": current_headers}

@app.route('/proxy')
def proxy():
    """Proxy per liste M3U che aggiunge automaticamente /proxy/m3u?url= con IP prima dei link"""
    m3u_url = request.args.get('url', '').strip()
    if not m3u_url:
        return "Errore: Parametro 'url' mancante", 400

    try:
        server_ip = request.host
        response = requests.get(m3u_url, timeout=(10, 30))
        response.raise_for_status()
        m3u_content = response.text
        
        modified_lines = []
        exthttp_headers_query_params = ""

        for line in m3u_content.splitlines():
            line = line.strip()
            if line.startswith('#EXTHTTP:'):
                try:
                    json_str = line.split(':', 1)[1].strip()
                    headers_dict = json.loads(json_str)
                    temp_params = []
                    for key, value in headers_dict.items():
                        encoded_key = quote(quote(key))
                        encoded_value = quote(quote(str(value)))
                        temp_params.append(f"h_{encoded_key}={encoded_value}")
                    if temp_params:
                        exthttp_headers_query_params = "%26" + "%26".join(temp_params)
                    else:
                        exthttp_headers_query_params = ""
                except Exception as e:
                    app.logger.error(f"Errore nel parsing di #EXTHTTP '{line}': {e}")
                    exthttp_headers_query_params = ""
                modified_lines.append(line)
            elif line and not line.startswith('#'):
                if 'pluto.tv' in line.lower():
                    modified_lines.append(line)
                    exthttp_headers_query_params = ""
                else:
                    encoded_line = quote(line, safe='')
                    modified_line = f"http://{server_ip}/proxy/m3u?url={encoded_line}{exthttp_headers_query_params}"
                    modified_lines.append(modified_line)
                    exthttp_headers_query_params = ""
            else:
                modified_lines.append(line)
        
        modified_content = '\n'.join(modified_lines)
        parsed_m3u_url = urlparse(m3u_url)
        original_filename = os.path.basename(parsed_m3u_url.path)
        
        return Response(modified_content, content_type="application/vnd.apple.mpegurl", headers={'Content-Disposition': f'attachment; filename="{original_filename}"'})
        
    except requests.RequestException as e:
        return f"Errore durante il download della lista M3U: {str(e)}", 500
    except Exception as e:
        return f"Errore generico: {str(e)}", 500

@app.route('/proxy/m3u')
def proxy_m3u():
    """Proxy per file M3U e M3U8 con supporto per proxy e caching."""
    m3u_url = request.args.get('url', '').strip()
    if not m3u_url:
        return "Errore: Parametro 'url' mancante", 400

    # Crea una chiave univoca per la cache basata sull'URL e sugli header specifici
    # Questo assicura che richieste con header diversi non usino la stessa cache
    cache_key_headers = "&".join(sorted([f"{k}={v}" for k, v in request.args.items() if k.lower().startswith("h_")]))
    cache_key = f"{m3u_url}|{cache_key_headers}"

    # Controlla se la risposta è già in cache
    if cache_key in M3U8_CACHE:
        app.logger.info(f"Cache HIT per M3U8: {m3u_url}")
        cached_response = M3U8_CACHE[cache_key]
        return Response(cached_response, content_type="application/vnd.apple.mpegurl; charset=utf-8")
    
    app.logger.info(f"Cache MISS per M3U8: {m3u_url}")

    default_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/33.0 Mobile/15E148 Safari/605.1.15",
        "Referer": "https://vavoo.to/",
        "Origin": "https://vavoo.to"
    }

    request_headers = {
        unquote(key[2:]).replace("_", "-"): unquote(value).strip()
        for key, value in request.args.items()
        if key.lower().startswith("h_")
    }
    
    headers = {**default_headers, **request_headers}

    processed_url = m3u_url

    try:
        result = resolve_m3u8_link(processed_url, headers)

        if not result["resolved_url"]:
            return "Errore: Impossibile risolvere l'URL in un M3U8 valido.", 500

        resolved_url = result["resolved_url"]
        current_headers_for_proxy = result["headers"]

        proxy_config = get_proxy_config_for_url(resolved_url)
        if proxy_config['proxies']:
            app.logger.debug(f"Proxy in uso per GET {resolved_url}")

        m3u_response = requests.get(resolved_url, headers=current_headers_for_proxy, 
                                   proxies=proxy_config['proxies'], allow_redirects=True, timeout=(10, 20),
                                   verify=proxy_config['verify'])
        m3u_response.raise_for_status()
        m3u_response.encoding = m3u_response.apparent_encoding or 'utf-8'
        m3u_content = m3u_response.text
        final_url = m3u_response.url

        file_type = detect_m3u_type(m3u_content)

        if file_type == "m3u":
            return Response(m3u_content, content_type="application/vnd.apple.mpegurl; charset=utf-8")

        parsed_url = urlparse(final_url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path.rsplit('/', 1)[0]}/"

        headers_query = "&".join([f"h_{quote(k)}={quote(v)}" for k, v in current_headers_for_proxy.items()])

        modified_m3u8 = []
        for line in m3u_content.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-KEY") and 'URI="' in line:
                line = replace_key_uri(line, headers_query)
            elif line and not line.startswith("#"):
                segment_url = urljoin(base_url, line)
                line = f"/proxy/ts?url={quote(segment_url)}&{headers_query}"
            modified_m3u8.append(line)

        modified_m3u8_content = "\n".join(modified_m3u8)
        
        # Salva il contenuto modificato nella cache prima di restituirlo
        M3U8_CACHE[cache_key] = modified_m3u8_content
        
        return Response(modified_m3u8_content, content_type="application/vnd.apple.mpegurl; charset=utf-8")

    except requests.RequestException as e:
        return f"Errore durante il download o la risoluzione del file: {str(e)}", 500
    except Exception as e:
        return f"Errore generico nella funzione proxy_m3u: {str(e)}", 500

@app.route('/proxy/ts')
def proxy_ts():
    """Proxy per segmenti .TS con caching, headers personalizzati e supporto proxy."""
    ts_url = request.args.get('url', '').strip()
    if not ts_url:
        return "Errore: Parametro 'url' mancante", 400

    # Controlla se il segmento è in cache
    if ts_url in TS_CACHE:
        app.logger.info(f"Cache HIT per TS: {ts_url}")
        # Restituisce il contenuto direttamente dalla cache
        return Response(TS_CACHE[ts_url], content_type="video/mp2t")

    app.logger.info(f"Cache MISS per TS: {ts_url}")

    headers = {
        unquote(key[2:]).replace("_", "-"): unquote(value).strip()
        for key, value in request.args.items()
        if key.lower().startswith("h_")
    }

    proxy_config = get_proxy_config_for_url(ts_url)
    if proxy_config['proxies']:
        app.logger.debug(f"Proxy in uso per {ts_url}")

    try:
        # Nota: stream=False per scaricare l'intero segmento e poterlo mettere in cache
        response = requests.get(ts_url, headers=headers, proxies=proxy_config['proxies'], stream=False, allow_redirects=True, timeout=(10, 30), verify=proxy_config['verify'])
        response.raise_for_status()
        
        ts_content = response.content
        
        # Salva il contenuto del segmento nella cache
        if ts_content:
            TS_CACHE[ts_url] = ts_content
        
        return Response(ts_content, content_type="video/mp2t")
    
    except requests.RequestException as e:
        return f"Errore durante il download del segmento TS: {str(e)}", 500

@app.route('/proxy/key')
def proxy_key():
    """Proxy per la chiave AES-128 con caching, header personalizzati e supporto proxy."""
    key_url = request.args.get('url', '').strip()
    if not key_url:
        return "Errore: Parametro 'url' mancante per la chiave", 400

    # Controlla se la chiave è in cache
    if key_url in KEY_CACHE:
        app.logger.info(f"Cache HIT per KEY: {key_url}")
        return Response(KEY_CACHE[key_url], content_type="application/octet-stream")

    app.logger.info(f"Cache MISS per KEY: {key_url}")

    headers = {
        unquote(key[2:]).replace("_", "-"): unquote(value).strip()
        for key, value in request.args.items()
        if key.lower().startswith("h_")
    }

    proxy_config = get_proxy_config_for_url(key_url)
    if proxy_config['proxies']:
        app.logger.debug(f"Proxy in uso per {key_url}")

    try:
        response = requests.get(key_url, headers=headers, proxies=proxy_config['proxies'], 
                              allow_redirects=True, timeout=(10, 20), verify=proxy_config['verify'])
        response.raise_for_status()
        
        key_content = response.content
        
        # Salva la chiave nella cache
        KEY_CACHE[key_url] = key_content
        
        return Response(key_content, content_type="application/octet-stream")
    
    except requests.RequestException as e:
        return f"Errore durante il download della chiave: {str(e)}", 500

@app.route('/')
def index():
    """Pagina principale che mostra un messaggio di benvenuto"""
    return "Proxy started!"

if __name__ == '__main__':
    print("--- SCRIPT INIZIALIZZATO, IN ATTESA DI AVVIARE IL SERVER FLASK ---", flush=True)
    print("--- AVVIO DEL SERVER FLASK (MODALITÀ DEBUG) ---", flush=True)
    
    # Legge la porta dalla variabile d'ambiente PORT, altrimenti usa 7860 come default
    port = int(os.environ.get('PORT', 3000))
    
    print(f"--- SERVER IN AVVIO SULLA PORTA {port} ---", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False)
