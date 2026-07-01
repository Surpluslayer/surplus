"""
relay_client.py : portable Unipile client that talks to the Surplus relay.

Runs on ANY machine (Mac, sandbox, VM) that can reach the Railway app on :443.
It never needs the Unipile DSN or API key — the Railway relay injects those
server-side. This machine only needs two env vars:

    RAILWAY_BASE             e.g. https://surplus-production.up.railway.app
    SURPLUS_INTERNAL_TOKEN   the shared relay token

Usage:
    export RAILWAY_BASE=https://surplus-production.up.railway.app
    export SURPLUS_INTERNAL_TOKEN=surplus_relay_xxxxx
    python3 relay_client.py accounts                 # smoke test
    python3 relay_client.py invite queue.csv         # send invites from a CSV

CSV format for `invite`:  columns  Name,LinkedIn   (LinkedIn = full profile URL)

Requires only: python3 + `pip install requests`.
"""
import csv, json, os, random, sys, time
import requests

BASE  = os.environ["RAILWAY_BASE"].rstrip("/")
TOKEN = os.environ["SURPLUS_INTERNAL_TOKEN"]
DSN   = f"{BASE}/internal/unipile"                 # relay prefix; /api/v1/... appended
LI_ACCOUNT = os.environ.get("LI_ACCOUNT_ID", "-iE9dE3tShmocs3MbEwYcg")  # Daniel's LinkedIn
H = {"X-Internal-Token": TOKEN, "accept": "application/json", "content-type": "application/json"}

NOTE = ("Hi {First}, I'm building Surplus, an AI agent that manages your client relationships "
        "for you, so no relationship ever goes cold and you turn the ones you have into more "
        "business, automatically. I'll be in NYC over the next couple of weeks and would love "
        "to grab coffee if you're interested.")

def slug(u): u = (u or "").strip().rstrip("/"); return u.split("/in/")[-1] if "/in/" in u else ""
def first(n): return (n or "").split()[0] if n else ""

def get(path, **params):
    return requests.get(f"{DSN}/{path}", headers=H, params=params, timeout=40)

def post(path, body):
    return requests.post(f"{DSN}/{path}", headers=H, data=json.dumps(body), timeout=40)

def cmd_accounts():
    r = get("api/v1/accounts")
    print(r.status_code)
    if r.status_code == 200:
        for a in r.json().get("items", []):
            print(" ", a.get("type"), a.get("id"))
    else:
        print(r.text[:300])

def cmd_invite(csv_path):
    rows = [r for r in csv.DictReader(open(csv_path)) if slug(r.get("LinkedIn", ""))]
    print(f"queue: {len(rows)}")
    for i, r in enumerate(rows, 1):
        note = NOTE.format(First=first(r["Name"]))
        p = get(f"api/v1/users/{slug(r['LinkedIn'])}", account_id=LI_ACCOUNT)
        if p.status_code != 200:
            print(f"{i} {r['Name']}: PROFILE_ERR {p.status_code}"); time.sleep(2); continue
        pj = p.json(); pid = pj.get("provider_id"); nd = pj.get("network_distance"); inv = pj.get("invitation")
        if nd == "FIRST_DEGREE":
            print(f"{i} {r['Name']}: ALREADY_CONNECTED"); continue
        if isinstance(inv, dict) and inv.get("status") == "PENDING":
            print(f"{i} {r['Name']}: ALREADY_PENDING"); continue
        s = post("api/v1/users/invite", {"account_id": LI_ACCOUNT, "provider_id": pid, "message": note})
        print(f"{i} {r['Name']}: {'INVITE_SENT' if s.status_code in (200,201) else 'ERR '+str(s.status_code)}")
        if i < len(rows): time.sleep(random.randint(30, 60))

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "accounts":
        cmd_accounts()
    elif sys.argv[1] == "invite" and len(sys.argv) > 2:
        cmd_invite(sys.argv[2])
    else:
        print("usage: python3 relay_client.py [accounts | invite <queue.csv>]")
