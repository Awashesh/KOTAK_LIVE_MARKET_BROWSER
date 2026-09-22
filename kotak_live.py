
import os, time, threading, asyncio
from datetime import datetime

class KotakLiveManager:
    """
    One broker connection per Gunicorn worker.
    Browser reads the in-memory cache; it never opens a Yahoo request every second.
    REST quotes (consumer-key only) are the safe fallback.
    WebSocket starts after the user enters current TOTP.
    """
    def __init__(self):
        self.lock=threading.RLock()
        self.cache={}
        self.connected=False
        self.authenticated=False
        self.last_error=""
        self.last_tick=None
        self.client=None
        self.thread=None
        self.stop_event=threading.Event()
        self.watch_symbols=["RELIANCE","TCS","HDFCBANK","ICICIBANK","INFY"]
        self.tokens={}
        self._rest_client=None

    def _env(self,name):
        return os.getenv(name,"").strip()

    def configured(self):
        return bool(self._env("NEO_CONSUMER_KEY"))

    def full_auth_configured(self):
        return all(self._env(x) for x in ("NEO_CONSUMER_KEY","NEO_MOBILE_NUMBER","NEO_UCC","NEO_MPIN"))

    def status(self):
        with self.lock:
            return {"configured":self.configured(),"full_auth_configured":self.full_auth_configured(),
                    "authenticated":self.authenticated,"connected":self.connected,
                    "last_tick":self.last_tick,"last_error":self.last_error,
                    "watchlist":list(self.watch_symbols)}

    def _client(self):
        from neo_api_client import NeoAPI
        return NeoAPI(consumer_key=self._env("NEO_CONSUMER_KEY"), environment="prod")

    def _normalize_symbol(self,s):
        s=str(s).upper().strip().replace(".NS","")
        return s[:-3] if s.endswith("-EQ") else s

    def _extract_search_rows(self,r):
        if isinstance(r,list): return r
        if isinstance(r,dict):
            for k in ("data","result","values"):
                if isinstance(r.get(k),list): return r[k]
        return []

    def resolve_token(self,symbol):
        sym=self._normalize_symbol(symbol)
        if sym in self.tokens: return self.tokens[sym]
        c=self._rest_client or self._client()
        self._rest_client=c
        r=c.search_scrip(exchange_segment="nse_cm", symbol=sym, expiry="", option_type="", strike_price="")
        rows=self._extract_search_rows(r)
        best=None
        for x in rows:
            ts=str(x.get("pTrdSymbol") or x.get("trading_symbol") or x.get("tradingSymbol") or x.get("pSymbolName") or "")
            if ts.upper() in (sym, sym+"-EQ") or ts.upper().startswith(sym+"-EQ"):
                best=x; break
        if best is None and rows: best=rows[0]
        if not best: raise RuntimeError(f"Kotak token not found for {sym}")
        token=(best.get("pSymbol") or best.get("instrument_token") or best.get("instrumentToken")
               or best.get("exchange_token") or best.get("token"))
        if token is None: raise RuntimeError(f"Instrument token missing for {sym}")
        self.tokens[sym]=str(token)
        return str(token)

    def rest_quotes(self,symbols):
        if not self.configured():
            raise RuntimeError("Render Environment me NEO_CONSUMER_KEY add nahi hai.")
        c=self._rest_client or self._client()
        self._rest_client=c
        inst=[]; names={}
        for s in symbols:
            try:
                tok=self.resolve_token(s)
                inst.append({"instrument_token":tok,"exchange_segment":"nse_cm"})
                names[str(tok)]=self._normalize_symbol(s)
            except Exception:
                continue
        if not inst: return {}
        r=c.quotes(instrument_tokens=inst, quote_type="all")
        rows=self._extract_search_rows(r) if isinstance(r,dict) else (r if isinstance(r,list) else [])
        ans={}
        for x in rows:
            tok=str(x.get("exchange_token") or x.get("instrument_token") or x.get("instrumentToken") or "")
            name=names.get(tok) or self._normalize_symbol(x.get("display_symbol",""))
            if not name: continue
            def num(v):
                try:return float(v)
                except:return None
            ans[name]={"symbol":name,"ltp":num(x.get("ltp") or x.get("last_traded_price")),
                       "change":num(x.get("change")),"pct":num(x.get("per_change") or x.get("percent_change")),
                       "volume":num(x.get("last_volume") or x.get("volume")),"source":"Kotak Neo REST",
                       "updated":datetime.now().strftime("%H:%M:%S")}
        with self.lock: self.cache.update(ans)
        return ans

    def set_watchlist(self,symbols):
        cleaned=[]
        for s in symbols:
            s=self._normalize_symbol(s)
            if s and s not in cleaned: cleaned.append(s)
        with self.lock: self.watch_symbols=cleaned[:40]
        # Active websocket will pick this up on next reconnect. REST cache works immediately.

    def snapshot(self):
        with self.lock:
            return {"quotes":dict(self.cache),"status":self.status()}

    def login_and_start(self,totp):
        if not self.full_auth_configured():
            raise RuntimeError("NEO_CONSUMER_KEY, NEO_MOBILE_NUMBER, NEO_UCC, NEO_MPIN Render Environment me add karein.")
        c=self._client()
        r1=c.totp_login(mobile_number=self._env("NEO_MOBILE_NUMBER"), ucc=self._env("NEO_UCC"), totp=totp)
        if isinstance(r1,dict) and (r1.get("error") or r1.get("Error")):
            raise RuntimeError(str(r1.get("error") or r1.get("Error")))
        r2=c.totp_validate(mpin=self._env("NEO_MPIN"))
        if isinstance(r2,dict) and (r2.get("error") or r2.get("Error")):
            raise RuntimeError(str(r2.get("error") or r2.get("Error")))
        self.client=c
        with self.lock:
            self.authenticated=True; self.last_error=""
        if not self.thread or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread=threading.Thread(target=self._thread_main,daemon=True,name="kotak-live")
            self.thread.start()

    def _thread_main(self):
        try: asyncio.run(self._ws_loop())
        except Exception as e:
            with self.lock:
                self.connected=False; self.last_error=str(e)

    async def _ws_loop(self):
        from neo_api_client.websocket.feed import WsToken, SFeedScrip, SFeedScripLite, SFeedIndex
        backoff=2
        while not self.stop_event.is_set():
            try:
                stock_tokens=[]
                for s in list(self.watch_symbols):
                    try: stock_tokens.append(WsToken("nse_cm",self.resolve_token(s)))
                    except Exception: pass
                async with self.client.create_websocket() as ws:
                    idx=[WsToken("nse_cm","Nifty 50"),WsToken("nse_cm","Nifty Bank"),WsToken("bse_cm","SENSEX")]
                    await ws.subscribe_index(idx)
                    if stock_tokens: await ws.subscribe_scrips_lite(stock_tokens)
                    with self.lock:
                        self.connected=True; self.last_error=""
                    backoff=2
                    async for m in ws:
                        if self.stop_event.is_set(): break
                        d=m.model_dump() if hasattr(m,"model_dump") else {}
                        sym=d.get("trading_symbol") or d.get("instrument_token") or "MARKET"
                        sym=self._normalize_symbol(sym)
                        # Friendly names for index tokens
                        token=str(d.get("instrument_token",""))
                        if token=="Nifty 50": sym="NIFTY 50"
                        elif token=="Nifty Bank": sym="BANK NIFTY"
                        elif token.upper()=="SENSEX": sym="SENSEX"
                        ltp=d.get("last_traded_price")
                        pct=d.get("percent_change", d.get("percentage_change"))
                        chg=d.get("change")
                        q={"symbol":sym,"ltp":ltp,"change":chg,"pct":pct,
                           "source":"Kotak Neo WebSocket","updated":datetime.now().strftime("%H:%M:%S")}
                        with self.lock:
                            self.cache[sym]=q; self.last_tick=q["updated"]
            except Exception as e:
                with self.lock:
                    self.connected=False; self.last_error=str(e)
                await asyncio.sleep(backoff)
                backoff=min(backoff*2,30)
