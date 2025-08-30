from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
import requests
import xml.etree.ElementTree as ET
from decimal import Decimal, getcontext
import time
import logging

# -------------------
# Logging
# -------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------
# Precision
# -------------------
getcontext().prec = 28

# -------------------
# FastAPI app
# -------------------
app = FastAPI(
    title="Crypto & Fiat Converter API",
    description="Fast converter for crypto (Binance) & fiat (Yahoo + ECB fallback) with caching",
    version="1.5.0",
)

# -------------------
# Cache
# -------------------
CACHE = {
    "ecb": {"data": None, "timestamp": 0},
    "binance": {"data": None, "timestamp": 0},
    "yahoo": {"data": {}, "timestamp": 0},
}
ECB_TTL = 60 * 60   # 1 hour
BINANCE_TTL = 10
YAHOO_TTL = 10

def fmt(n: Decimal, decimals=8) -> str:
    return format(n.quantize(Decimal(10) ** -decimals), 'f')


# -------------------
# ECB rates
# -------------------
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

def get_ecb_rates():
    now = time.time()
    if CACHE["ecb"]["data"] and now - CACHE["ecb"]["timestamp"] < ECB_TTL:
        return CACHE["ecb"]["data"]

    try:
        response = requests.get(ECB_URL, timeout=5)
        root = ET.fromstring(response.content)
        ns = {"gesmes": "http://www.gesmes.org/xml/2002-08-01",
              "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
        rates = {"EUR": Decimal("1.0")}
        for cube in root.findall(".//ecb:Cube[@currency]", ns):
            currency = cube.attrib["currency"]
            rates[currency] = Decimal(cube.attrib["rate"])
        CACHE["ecb"] = {"data": rates, "timestamp": now}
        return rates
    except Exception as e:
        logger.error(f"ECB fetch failed: {e}")
        if CACHE["ecb"]["data"]:
            return CACHE["ecb"]["data"]
        raise HTTPException(status_code=503, detail="ECB service unavailable")


# -------------------
# Yahoo Forex
# -------------------
def get_yahoo_rate(base: str, quote: str):
    base, quote = base.upper(), quote.upper()
    pair = f"{base}{quote}=X"
    now = time.time()

    if pair in CACHE["yahoo"]["data"] and now - CACHE["yahoo"]["timestamp"] < YAHOO_TTL:
        return CACHE["yahoo"]["data"][pair]

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}"
    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        value = Decimal(str(price))
        CACHE["yahoo"]["data"][pair] = value
        CACHE["yahoo"]["timestamp"] = now
        return value
    except Exception as e:
        logger.warning(f"Yahoo rate fetch failed for {pair}: {e}")
        return None


# -------------------
# Binance Crypto
# -------------------
BINANCE_URL_ALL = "https://api.binance.com/api/v3/ticker/price"

def get_binance_prices():
    now = time.time()
    if CACHE["binance"]["data"] and now - CACHE["binance"]["timestamp"] < BINANCE_TTL:
        return CACHE["binance"]["data"]

    try:
        r = requests.get(BINANCE_URL_ALL, timeout=5)
        if r.status_code != 200:
            return {}
        data = {item["symbol"]: Decimal(item["price"]) for item in r.json()}
        CACHE["binance"] = {"data": data, "timestamp": now}
        return data
    except Exception as e:
        logger.warning(f"Binance fetch failed: {e}")
        return {}


# -------------------
# Conversion Endpoint
# -------------------
@app.get("/convert")
def convert(
    from_currency: str = Query(..., alias="from"),
    to_currency: str = Query(..., alias="to"),
    amount: float = Query(1.0)
):
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    amount = Decimal(str(amount))

    ecb_rates = get_ecb_rates()
    binance_prices = get_binance_prices()

    # Crypto → Crypto
    if from_currency not in ecb_rates and to_currency not in ecb_rates:
        direct_price = binance_prices.get(from_currency + to_currency)
        if direct_price:
            converted = amount * direct_price
            return {"from": from_currency, "to": to_currency,
                    "amount": fmt(amount), "rate": fmt(direct_price),
                    "converted": fmt(converted), "via": "direct"}

        from_usd = binance_prices.get(from_currency + "USDT")
        to_usd = binance_prices.get(to_currency + "USDT")
        if not from_usd or not to_usd:
            raise HTTPException(status_code=400,
                                detail=f"Unsupported crypto pair: {from_currency}/{to_currency}")
        usd_value = amount * from_usd
        converted = usd_value / to_usd
        rate = from_usd / to_usd
        return {"from": from_currency, "to": to_currency,
                "amount": fmt(amount), "rate": fmt(rate),
                "converted": fmt(converted), "via": "USDT"}

    # Crypto → Fiat
    if from_currency not in ecb_rates and to_currency in ecb_rates:
        crypto_price_usd = binance_prices.get(from_currency + "USDT")
        if not crypto_price_usd:
            raise HTTPException(status_code=400,
                                detail=f"Unsupported crypto: {from_currency}")
        usd_amount = amount * crypto_price_usd
        if to_currency == "USD":
            return {"from": from_currency, "to": to_currency,
                    "amount": fmt(amount), "rate": fmt(crypto_price_usd),
                    "converted": fmt(usd_amount), "via": "binance"}

        yahoo_rate = get_yahoo_rate("USD", to_currency)
        if yahoo_rate:
            converted = usd_amount * yahoo_rate
            return {"from": from_currency, "to": to_currency,
                    "amount": fmt(amount), "rate": fmt(crypto_price_usd * yahoo_rate),
                    "converted": fmt(converted), "via": "binance+yahoo"}

        usd_to_eur = Decimal("1") / ecb_rates["USD"]
        eur_amount = usd_amount * usd_to_eur
        converted = eur_amount * ecb_rates[to_currency]
        rate = crypto_price_usd * usd_to_eur * ecb_rates[to_currency]
        return {"from": from_currency, "to": to_currency,
                "amount": fmt(amount), "rate": fmt(rate),
                "converted": fmt(converted), "via": "binance+ecb"}

    # Fiat → Crypto
    if from_currency in ecb_rates and to_currency not in ecb_rates:
        crypto_price_usd = binance_prices.get(to_currency + "USDT")
        if not crypto_price_usd:
            raise HTTPException(status_code=400,
                                detail=f"Unsupported target crypto: {to_currency}")

        yahoo_rate = get_yahoo_rate(from_currency, "USD")
        usd_amount = amount * yahoo_rate if yahoo_rate else (amount / ecb_rates[from_currency] * ecb_rates["USD"])
        converted = usd_amount / crypto_price_usd
        rate = usd_amount / (amount * crypto_price_usd)
        return {"from": from_currency, "to": to_currency,
                "amount": fmt(amount), "rate": fmt(rate),
                "converted": fmt(converted),
                "via": "yahoo+binance" if yahoo_rate else "ecb+binance"}

    # Fiat → Fiat
    if from_currency in ecb_rates and to_currency in ecb_rates:
        yahoo_rate = get_yahoo_rate(from_currency, to_currency)
        if yahoo_rate:
            converted = amount * yahoo_rate
            return {"from": from_currency, "to": to_currency,
                    "amount": fmt(amount), "rate": fmt(yahoo_rate),
                    "converted": fmt(converted), "via": "yahoo"}

        eur_amount = amount / ecb_rates[from_currency]
        converted = eur_amount * ecb_rates[to_currency]
        rate = ecb_rates[to_currency] / ecb_rates[from_currency]
        return {"from": from_currency, "to": to_currency,
                "amount": fmt(amount), "rate": fmt(rate),
                "converted": fmt(converted), "via": "ecb"}

    raise HTTPException(status_code=400, detail=f"Unsupported conversion: {from_currency} -> {to_currency}")
