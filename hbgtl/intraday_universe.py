"""Candidate pool and eligibility rules for the intraday sleeve.

The pool is a predeclared list of widely traded U.S.-listed common stocks (and ADRs) on NYSE/Nasdaq/Arca/
NYSE American. It was chosen for liquidity, NOT for past returns. Price, average volume, relative volume,
spread and data quality are re-checked every day from data, so a name that no longer qualifies is skipped.
Known bias: the list is chosen today, so a backtest over it has mild survivorship bias (names that delisted
or collapsed are under-represented).
"""
from __future__ import annotations

RESEARCH_POOL = sorted(set("""
F T PFE BAC INTC SOFI PLTR AAL CCL NIO SNAP RIVN LCID HOOD UBER WBD KMI CSCO KO VZ CMCSA WFC C GM DAL UAL
NKE SBUX PYPL JD BABA PBR VALE ITUB BBD NU GRAB AFRM MARA RIOT CLSK DKNG RBLX ROKU NCLH RCL PINS LYFT CHWY
GME NOK ERIC KGC AG HL CDE CLF AA FCX OXY DVN HAL SLB APA BTU ET EPD KDP MO BMY GILD MRNA CVS KHC HPE HPQ
SMCI ON MRVL AMD MU DIS XOM USB SCHW MS KVUE BSX ABEV GOLD BBVA ING HBAN KEY RF CFG FITB TFC AGNC NLY
JBLU LUV ALK AMCR BEN IVZ WMB PCG EXC FE PPL AES NEE DUK SO D CNP CSX UNP OKE TSN CAG GIS HRL KR WMT TGT
DG DLTR BBY M KSS GPS ANF AEO URBN TJX ROST VFC HBI UAA LEVI PATH U DOCU ZM TWLO NET OKTA DDOG MDB SNOW
CRWD ZS PANW S IONQ RGTI QBTS SOUN BBAI ACHR JOBY RKLB ASTS LUMN APLD CIFR IREN WULF HUT BITF CORZ
""".split()))

# Never trade these (ETFs, leveraged or inverse products) even if a screener returns them.
NAME_DENY = ("ETF", "ETN", "TRUST", "FUND", "2X", "3X", "ULTRA", "BULL", "BEAR", "INVERSE", "LEVERAGED", "DAILY", "ULTRAPRO",
             "PROSHARES", "DIREXION", "ISHARES", "SPDR", "INDEX")
EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}


def asset_ok(a: dict) -> str | None:
    """Return a rejection reason, or None if the asset may be traded by the intraday sleeve."""
    if not a:
        return "asset not found"
    from .equity_risk import LEVERAGED_OR_INVERSE
    if a.get("symbol") in LEVERAGED_OR_INVERSE:
        return "ETF / leveraged or inverse product excluded"
    if a.get("class", "us_equity") != "us_equity":
        return "not a U.S. equity"
    if not a.get("tradable", False) or a.get("status", "active") != "active":
        return "not tradable"
    if a.get("exchange") not in EXCHANGES:
        return f"exchange {a.get('exchange')} not allowed (OTC excluded)"
    words = set("".join(ch if ch.isalnum() else " " for ch in (a.get("name") or "").upper()).split())
    if words & set(NAME_DENY):
        return "ETF / leveraged or inverse product excluded"
    return None
