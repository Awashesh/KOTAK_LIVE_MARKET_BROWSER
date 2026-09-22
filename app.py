
import os, io, time, threading
from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import core
from kotak_live import KotakLiveManager

app = Flask(__name__)
live = KotakLiveManager()

def ok(data=None, **kw):
    p={"ok":True}
    if data is not None: p["data"]=data
    p.update(kw)
    return jsonify(p)

def fail(e, code=400):
    return jsonify({"ok":False,"error":str(e)}), code

def records(df):
    return df.where(pd.notnull(df), None).to_dict(orient="records")

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/health")
def health():
    return ok({"app":"Kotak Live Market Browser","kotak":live.status()})

@app.get("/api/kotak/status")
def kotak_status():
    return ok(live.status())

@app.post("/api/kotak/login")
def kotak_login():
    try:
        j=request.get_json(silent=True) or {}
        totp=str(j.get("totp","")).strip()
        if len(totp)!=6 or not totp.isdigit():
            return fail("6-digit current TOTP enter karein.")
        live.login_and_start(totp)
        return ok(live.status())
    except Exception as e:
        return fail(e)

@app.get("/api/live")
def live_cache():
    return ok(live.snapshot())

@app.post("/api/live/watch")
def live_watch():
    try:
        j=request.get_json(silent=True) or {}
        symbols=j.get("symbols",[])
        if not isinstance(symbols,list): return fail("symbols must be a list")
        live.set_watchlist(symbols[:40])
        return ok(live.snapshot())
    except Exception as e:
        return fail(e)

@app.get("/api/quotes")
def quotes():
    """Fast Kotak REST quote fallback. Does not call Yahoo every second."""
    try:
        syms=[x.strip().upper() for x in request.args.get("symbols","RELIANCE,TCS,HDFCBANK").split(",") if x.strip()]
        return ok(live.rest_quotes(syms[:40]))
    except Exception as e:
        return fail(e)

@app.get("/api/suggest/equity")
def suggest_equity():
    try:
        q=request.args.get("q","").strip()
        return ok(core.suggest_equities(q, 12) if q else [])
    except Exception as e: return fail(e)

@app.get("/api/suggest/fund")
def suggest_fund():
    try:
        q=request.args.get("q","").strip()
        return ok(core.suggest_funds(q, 12) if q else [])
    except Exception as e: return fail(e)

@app.get("/api/analyze/equity")
def analyze_equity():
    try:
        ticker=request.args.get("ticker","").strip()
        if not ticker: return fail("Ticker required")
        if not ticker.startswith("^") and "." not in ticker: ticker += ".NS"
        r=core.analyze_equity(ticker)
        row=dict(r["row"])
        return ok({"kind":"equity","name":r["name"],"id":r["id"],"row":row,
                   "info":r.get("info",{}),"news":r.get("news",[])[:10],
                   "dividends":r.get("dividends",[]),
                   "chart":[{"date":d.strftime("%Y-%m-%d"),"value":round(float(v),4)}
                            for d,v in r["series"].dropna().tail(1300).items()]})
    except Exception as e: return fail(e)

@app.get("/api/analyze/fund")
def analyze_fund():
    try:
        code=request.args.get("code","").strip()
        if not code: return fail("Scheme code required")
        r=core.analyze_fund(code)
        return ok({"kind":"fund","name":r["name"],"id":str(r["id"]),"row":dict(r["row"]),
                   "info":r.get("info",{}),"news":r.get("news",[])[:10],
                   "chart":[{"date":d.strftime("%Y-%m-%d"),"value":round(float(v),4)}
                            for d,v in r["series"].dropna().tail(1800).items()]})
    except Exception as e: return fail(e)

@app.get("/api/stocks")
def stocks():
    try:
        universe=request.args.get("universe","nifty50")
        period=request.args.get("period","6m")
        top=min(max(int(request.args.get("top",20)),1),100)
        mg=request.args.get("min_growth","").strip()
        mg=float(mg) if mg else None
        consistent=request.args.get("consistent","0")=="1"
        # Web deployment: skip slow per-row news/dividend calls in screener.
        df,buckets=core.screen_stocks(universe,period,mg,top,consistent,False,False)
        return ok(records(df), buckets=buckets)
    except Exception as e: return fail(e,500)

@app.get("/api/funds")
def funds():
    try:
        q=request.args.get("q","flexi cap").strip()
        period=request.args.get("period","1y")
        top=min(max(int(request.args.get("top",20)),1),50)
        mg=request.args.get("min_growth","").strip()
        mg=float(mg) if mg else None
        scan=min(max(int(request.args.get("scan",30)),5),80)
        df,buckets=core.screen_funds(q,period,mg,top,scan,False,False)
        return ok(records(df), buckets=buckets)
    except Exception as e: return fail(e,500)

@app.post("/api/export")
def export_excel():
    try:
        j=request.get_json(silent=True) or {}
        rows=j.get("rows",[])
        if not rows: return fail("No rows to export")
        bio=io.BytesIO()
        pd.DataFrame(rows).to_excel(bio,index=False,engine="openpyxl")
        bio.seek(0)
        return send_file(bio,as_attachment=True,download_name="market_report.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e: return fail(e)

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False,threaded=True)
