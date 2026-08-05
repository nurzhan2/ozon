# -*- coding: utf-8 -*-
"""Офлайн-проверка правок performance_api / collector: сеть подменена."""
import os, sys, json, shutil, logging, tempfile

CACHE = tempfile.mkdtemp(prefix="perfcache_")
os.environ["PERF_CACHE_DIR"] = CACHE
os.environ["PERF_POLL_START"] = "0"      # без реальных пауз в тесте
os.environ["PERF_POLL_MAX"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

from ozon import performance_api as PA


class Resp:
    def __init__(self, code, body="", data=None):
        self.status_code = code
        self._data = data
        self.text = body if data is None else json.dumps(data, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._data if self._data is not None else json.loads(self.text or "{}")


class FakeSession:
    """Мини-эмулятор Performance API."""
    def __init__(self, campaigns, forbidden=(), daily_limit=None, polls=1):
        self.headers = {}
        self.campaigns = campaigns
        self.forbidden = set(str(x) for x in forbidden)
        self.daily_limit = daily_limit
        self.polls = polls
        self.calls = []
        self.n = 0
        self._reports = {}
        self._poll_left = {}

    def _bump(self, path):
        self.n += 1
        self.calls.append(path)
        if self.daily_limit and self.n > self.daily_limit:
            return Resp(429, '{"error":"Превышен дневной лимит запросов (максимум 2000)"}')
        return None

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def request(self, method, url, **kw):
        path = url.split("api-performance.ozon.ru")[-1]
        over = self._bump(path)
        if over is not None:
            return over

        if path.endswith("/api/client/token"):
            return Resp(200, data={"access_token": "T", "expires_in": 1800})

        if path.endswith("/api/client/campaign"):
            return Resp(200, data={"list": self.campaigns})

        if path.endswith("/api/client/statistics"):
            ids = [str(c) for c in kw["json"]["campaigns"]]
            if any(i in self.forbidden for i in ids):
                return Resp(400, '{"error":"rpc error: code = InvalidArgument desc = '
                                 'generation of this type of report is forbidden for '
                                 'the transferred list of campaigns"}')
            uid = f"u{len(self._reports)}"
            self._reports[uid] = (ids, kw["json"]["dateFrom"], kw["json"]["dateTo"])
            self._poll_left[uid] = self.polls
            return Resp(200, data={"UUID": uid})

        if "/api/client/statistics/report" in path:
            uid = kw["params"]["UUID"]
            ids, df, dt = self._reports[uid]
            rows = ["sku;Дата;Расход, руб."]
            from datetime import date, timedelta
            d0 = date.fromisoformat(df)
            d1 = date.fromisoformat(dt)
            cur = d0
            while cur <= d1:
                for i in ids:
                    rows.append(f"SKU{i};{cur.strftime('%d.%m.%Y')};10,00")
                cur += timedelta(days=1)
            return Resp(200, "\r\n".join(rows) + "\r\n")

        if "/api/client/statistics/" in path:
            uid = path.rsplit("/", 1)[-1]
            self._poll_left[uid] -= 1
            state = "OK" if self._poll_left[uid] <= 0 else "IN_PROGRESS"
            return Resp(200, data={"state": state})

        return Resp(404, "no route")


def fresh(client="cid", **kw):
    api = PA.PerformanceAPI(client, "secret", name="ТЕСТ")
    api.session = FakeSession(**kw)
    return api


def camps(spec):
    """spec: [(id, state, toDate)]"""
    return [{"id": i, "state": s, "toDate": t} for i, s, t in spec]


ok = True
def check(label, cond, extra=""):
    global ok
    print(("  OK   " if cond else "  ПРОВАЛ ") + label + ("" if cond else f"  <- {extra}"))
    ok = ok and cond


# ---------------------------------------------------------------- 1. фильтры
print("\n1. Отбор кампаний")
spec = ([(str(1000 + i), "CAMPAIGN_STATE_RUNNING", "") for i in range(5)]
        + [(str(2000 + i), "CAMPAIGN_STATE_ARCHIVED", "") for i in range(4)]
        + [(str(3000 + i), "CAMPAIGN_STATE_INACTIVE", "") for i in range(3)]
        + [("4000", "CAMPAIGN_STATE_STOPPED", "2026-07-01")])
api = fresh(campaigns=camps(spec))
kept = api.campaigns("2026-08-01", "2026-08-04")
check("архивные отсеяны", not any(k.startswith("2") for k in kept), kept)
check("неактивные оставлены (могли тратить в начале периода)",
      sum(1 for k in kept if k.startswith("3")) == 3, kept)
check("завершившиеся до начала периода отсеяны", "4000" not in kept, kept)
check("работающие на месте", sum(1 for k in kept if k.startswith("1")) == 5, kept)
check("список кампаний берётся один раз",
      api.session.calls.count("/api/client/campaign") == 1)
api.campaigns("2026-08-01", "2026-08-04")
check("повторный вызов не тратит запрос",
      api.session.calls.count("/api/client/campaign") == 1)

# ------------------------------------------------------- 2. запрет отчёта
print("\n2. Запрещённые кампании (400 InvalidArgument)")
spec2 = [(str(5000 + i), "CAMPAIGN_STATE_RUNNING", "") for i in range(20)]
bad = ["5003", "5011"]
api = fresh(client="cid2", campaigns=camps(spec2), forbidden=bad)
rows = api.statistics("2026-08-01", "2026-08-01")
got = {r["sku"] for r in rows}
check("плохие кампании исключены из результата",
      "SKU5003" not in got and "SKU5011" not in got, sorted(got))
check("18 хороших кампаний собраны, а не потеряны вместе с пачкой",
      len(got) == 18, len(got))
check("плохие занесены в чёрный список", api._forbidden == set(bad), api._forbidden)
saved = json.load(open(os.path.join(CACHE, "perf_forbidden_cid2.json")))
check("чёрный список записан на диск", set(saved["ids"]) == set(bad), saved)

# повторный запуск с тем же client_id должен их не запрашивать
api2 = fresh(client="cid2", campaigns=camps(spec2), forbidden=bad)
n_before = api2.session.n
rows2 = api2.statistics("2026-08-01", "2026-08-01")
posts = [c for c in api2.session.calls if c.endswith("/api/client/statistics")]
check("во второй запуск ни один запрос не упёрся в 400",
      len({r["sku"] for r in rows2}) == 18 and len(posts) == 2,
      f"постов {len(posts)}")

# ------------------------------------------------------- 3. суточный лимит
print("\n3. Суточный лимит запросов")
api = fresh(client="cid3", campaigns=camps(spec2), daily_limit=6)
rows = api.statistics("2026-08-01", "2026-08-01")
check("сбор прерван, а не зациклен на повторах", api.session.n <= 8, api.session.n)
check("флаг исчерпания выставлен", api._quota_hit)
n_at_stop = api.session.n
api.statistics("2026-08-01", "2026-08-01")
check("следующий вызов не делает ни одного запроса",
      api.session.n == n_at_stop, api.session.n)
api3 = fresh(client="cid3", campaigns=camps(spec2), daily_limit=100)
check("исчерпание пережило пересоздание клиента (файл на диске)",
      api3._usage.get("blocked") is True, api3._usage)

# --------------------------------------------------- 4. экономия на опросах
print("\n4. Опрос статуса с нарастающей паузой")
os.environ.pop("PERF_POLL_START"); os.environ.pop("PERF_POLL_MAX")
import importlib
PA2 = importlib.reload(PA)
PA2.POLL_START, PA2.POLL_MAX, PA2.POLL_GROWTH = 0.0, 0.0, 1.6
api = PA2.PerformanceAPI("cid4", "s", name="ТЕСТ")
api.session = FakeSession(campaigns=camps(spec2[:10]), polls=8)
api.statistics("2026-08-01", "2026-08-01")
status = [c for c in api.session.calls if c.startswith("/api/client/statistics/u")]
check("опросы статуса считаются и ограничены", len(status) == 8, len(status))

# ------------------------------------------------- 5. отсев давно мёртвых
print("\n5. Отсев неработающих и давно не менявшихся (PERF_STALE_DAYS)")
A = PA2.PerformanceAPI
old_inactive = {"id": "1", "state": "CAMPAIGN_STATE_INACTIVE",
                "createdAt": "2024-02-08T14:27:56Z", "updatedAt": "2024-03-10T16:35:08Z"}
old_running = {"id": "2", "state": "CAMPAIGN_STATE_RUNNING",
               "createdAt": "2024-02-08T14:27:56Z", "updatedAt": "2024-03-10T16:35:08Z"}
fresh_inactive = {"id": "3", "state": "CAMPAIGN_STATE_INACTIVE",
                  "createdAt": "2024-01-01T00:00:00Z", "updatedAt": "2026-08-03T10:00:00Z"}
nodates = {"id": "4", "state": "CAMPAIGN_STATE_INACTIVE"}
DF = "2026-08-01"
check("выключено по умолчанию — ничего не режет", not A.is_stale(old_inactive, DF, 0))
check("старая неактивная отсеивается", A.is_stale(old_inactive, DF, 7))
check("работающая не трогается никогда", not A.is_stale(old_running, DF, 7))
check("неактивная, но менялась внутри периода — остаётся",
      not A.is_stale(fresh_inactive, DF, 7))
check("без дат не режем", not A.is_stale(nodates, DF, 7))
check("last_touch берёт максимум по всем полям",
      A.last_touch(fresh_inactive) == "2026-08-03", A.last_touch(fresh_inactive))
check("запас в днях работает: остановлена за 2 дня до периода при запасе 7 — остаётся",
      not A.is_stale({"id": "5", "state": "CAMPAIGN_STATE_INACTIVE",
                      "updatedAt": "2026-07-30T00:00:00Z"}, DF, 7))

print("\nИТОГ:", "все проверки пройдены" if ok else "ЕСТЬ ПРОВАЛЫ")
shutil.rmtree(CACHE, ignore_errors=True)
sys.exit(0 if ok else 1)
