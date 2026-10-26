"""Import a trusted operator-owned catalog, without exposing public admin routes."""
import argparse
import json
from datetime import datetime
from booking import SCHEMA, connection


def load_catalog(path, catalog):
    masters, services, windows = catalog['masters'], catalog['services'], catalog['availability']
    ids = {m['id'] for m in masters}
    if len(ids) != len(masters) or any(not m['id'] or not m['name'] for m in masters):
        raise ValueError('Master IDs must be unique and names nonempty')
    if len({s['id'] for s in services}) != len(services):
        raise ValueError('Service IDs must be unique')
    for s in services:
        if s['master_id'] not in ids or type(s['duration']) is not int or not 60 <= s['duration'] <= 86400 or type(s['buffer']) is not int or not 0 <= s['buffer'] <= 86400:
            raise ValueError('Invalid service master, duration or buffer (seconds)')
    intervals = []
    for w in windows:
        start, end = datetime.fromisoformat(w['start']), datetime.fromisoformat(w['end'])
        if w['master_id'] not in ids or start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError('Invalid availability or missing timezone')
        intervals.append((w['master_id'],int(start.timestamp()),int(end.timestamp())))
    with connection(path) as db:
        db.executescript(SCHEMA)
    with connection(path, True) as db:
        for m in masters:
            db.execute('INSERT INTO masters VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (m['id'],m['name']))
        for s in services:
            old = db.execute('SELECT master_id FROM services WHERE id=?', (s['id'],)).fetchone()
            if old and old['master_id'] != s['master_id']:
                raise ValueError('Cannot transfer a service to another master')
            db.execute('INSERT INTO services VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,duration=excluded.duration,buffer=excluded.buffer',
                       (s['id'],s['master_id'],s['name'],s['duration'],s['buffer']))
        for window in intervals:
            db.execute('INSERT INTO availability VALUES (?,?,?) ON CONFLICT(master_id,start) DO UPDATE SET end=excluded.end', window)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('catalog')
    parser.add_argument('--db', default='booking.db')
    args = parser.parse_args()
    with open(args.catalog, encoding='utf-8') as source:
        load_catalog(args.db, json.load(source))
