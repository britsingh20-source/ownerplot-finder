from __future__ import annotations
import json, sqlite3, time
from dataclasses import asdict
from pathlib import Path
from .domain import Listing, SearchQuery, SellerType


class ListingCache:
    def __init__(self,path,ttl_seconds=86400):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.ttl_seconds=ttl_seconds
        with sqlite3.connect(self.path) as db: db.execute("CREATE TABLE IF NOT EXISTS searches (cache_key TEXT PRIMARY KEY, created_at INTEGER NOT NULL, payload TEXT NOT NULL)")
    @staticmethod
    def key(query): return f"{query.locality.casefold()}|{query.property_type}|{query.max_price}|{query.transaction}"
    def get(self,query):
        with sqlite3.connect(self.path) as db: row=db.execute("SELECT created_at,payload FROM searches WHERE cache_key=?",(self.key(query),)).fetchone()
        if not row or int(time.time())-row[0]>self.ttl_seconds: return None
        records=json.loads(row[1])
        for record in records: record["seller_type"]=SellerType(record["seller_type"])
        return [Listing(**record) for record in records]
    def put(self,query,listings):
        payload=json.dumps([asdict(item) for item in listings])
        with sqlite3.connect(self.path) as db: db.execute("INSERT OR REPLACE INTO searches(cache_key,created_at,payload) VALUES(?,?,?)",(self.key(query),int(time.time()),payload))


class WatchStore:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS watches (chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,locality TEXT NOT NULL,max_price INTEGER,active INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(chat_id,locality))")
            db.execute("CREATE TABLE IF NOT EXISTS seen_listings (chat_id INTEGER NOT NULL,locality TEXT NOT NULL,fingerprint TEXT NOT NULL,first_seen INTEGER NOT NULL,PRIMARY KEY(chat_id,locality,fingerprint))")
    def add(self,chat_id,user_id,query):
        with sqlite3.connect(self.path) as db: db.execute("INSERT OR REPLACE INTO watches(chat_id,user_id,locality,max_price,active) VALUES(?,?,?,?,1)",(chat_id,user_id,query.locality,query.max_price))
    def remove(self,chat_id,locality):
        with sqlite3.connect(self.path) as db: return db.execute("DELETE FROM watches WHERE chat_id=? AND lower(locality)=lower(?)",(chat_id,locality)).rowcount>0
    def list(self,chat_id=None):
        sql="SELECT chat_id,user_id,locality,max_price FROM watches WHERE active=1"; params=()
        if chat_id is not None: sql+=" AND chat_id=?"; params=(chat_id,)
        with sqlite3.connect(self.path) as db: rows=db.execute(sql,params).fetchall()
        return [(r[0],r[1],SearchQuery(locality=r[2],max_price=r[3])) for r in rows]
    def new_fingerprints(self,chat_id,locality,fingerprints):
        now=int(time.time())
        with sqlite3.connect(self.path) as db:
            existing={r[0] for r in db.execute("SELECT fingerprint FROM seen_listings WHERE chat_id=? AND locality=?",(chat_id,locality))}
            new=[value for value in fingerprints if value not in existing]
            db.executemany("INSERT OR IGNORE INTO seen_listings(chat_id,locality,fingerprint,first_seen) VALUES(?,?,?,?)",[(chat_id,locality,value,now) for value in fingerprints])
        return new
