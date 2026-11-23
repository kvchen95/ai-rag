#!/usr/bin/env python3
"""Create / verify the Qdrant collection used by ai-rag (方案B).

  * collection: kb_chunks
  * dense      : named vector "dense", size 512, Cosine   (bge-small-zh-v1.5)
  * sparse     : named vector "bm25", modifier=IDF        (Qdrant server-side BM25)
  * payload indexes: doc_key / version_id / chunk_id / chunk_type / status /
                     parent_id / is_public / allowed_depts / allowed_roles /
                     effective_year / page_num

Idempotent by design:
  * missing collection  -> create
  * existing compatible -> only add missing payload indexes
  * existing incompatible (wrong dim / distance / no idf) -> report and exit 2,
    because mixed dims cannot coexist: pass --recreate to drop and rebuild
    (destructive: points must be re-populated from PG, see qdrant_rebuild.py).

Run inside the compose network (or on any host that can reach Qdrant):

    docker compose exec -T api python scripts/qdrant_init.py
    docker compose exec -T api python scripts/qdrant_init.py --dry-run
    docker compose exec -T api python scripts/qdrant_init.py --recreate
"""

from __future__ import annotations

import argparse
import os
import sys

import qdrant_common as qc


def env_url(name: str, default: str) -> str:
    return os.getenv(name, default)


def main() -> int:
    ap = argparse.ArgumentParser(description="create/verify the kb_chunks collection")
    ap.add_argument("--qdrant-url", default=env_url("QDRANT_URL", "http://127.0.0.1:6333"))
    ap.add_argument("--collection", default=env_url("QDRANT_COLLECTION", qc.DEFAULT_COLLECTION))
    ap.add_argument("--dim", type=int, default=int(env_url("EMBEDDING_DIM", str(qc.DEFAULT_DIM))))
    ap.add_argument("--recreate", action="store_true",
                    help="drop the collection first (points are lost; rebuild from PG afterwards)")
    ap.add_argument("--dry-run", action="store_true", help="report what would change, touch nothing")
    args = ap.parse_args()

    base, name, dim = args.qdrant_url.rstrip("/"), args.collection, args.dim

    try:
        info = qc.collection_info(base, name)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot reach Qdrant at {base}: {exc}", file=sys.stderr)
        return 3

    print(f"qdrant : {base}")
    print(f"target : collection={name} dense={qc.DENSE_VECTOR}/{dim}/Cosine "
          f"sparse={qc.SPARSE_VECTOR}/idf")

    if info is not None:
        problems = qc.spec_mismatches(info, dim)
        pts = info.get("points_count")
        print(f"state  : exists (points={pts}, status={info.get('status')})")
        if problems:
            for p in problems:
                print(f"  MISMATCH: {p}")
            if not args.recreate:
                print("\nRefusing to touch an incompatible collection.")
                print("Fix: re-run with --recreate (drops points), then rebuild: "
                      "python scripts/qdrant_rebuild.py")
                return 2
            if args.dry_run:
                print("\ndry-run: would DELETE and re-CREATE the collection + payload indexes")
                return 0
            print("action : --recreate -> dropping collection")
            qc.delete_collection(base, name)
            info = None
        else:
            print("spec   : OK (dense 512/Cosine + bm25/idf)")
    else:
        print("state  : missing")

    if info is None:
        if args.dry_run:
            print("action : would CREATE the collection")
            print(f"payload: would CREATE {len(qc.PAYLOAD_INDEXES)} indexes "
                  f"({', '.join(qc.PAYLOAD_INDEXES)})")
            print("dry-run: nothing executed")
            return 0
        qc.create_collection(base, name, dim)
        print("action : collection created")
        created = qc.ensure_payload_indexes(base, name)
        print(f"payload: created {len(created)} index(es) {created}")
    else:
        if args.dry_run:
            existing = set((info.get("payload_schema") or {}).keys())
            missing = [f for f in qc.PAYLOAD_INDEXES if f not in existing]
            print(f"action : would CREATE {len(missing)} missing payload index(es) {missing}")
            print("dry-run: nothing executed")
            return 0
        created = qc.ensure_payload_indexes(base, name)
        print(f"payload: created {len(created)} index(es) {created or '(all present)'}")

    # Final verification: re-read and assert the shape we rely on at query time.
    info = qc.collection_info(base, name)
    problems = qc.spec_mismatches(info or {}, dim)
    if problems:
        for p in problems:
            print(f"  ERROR: {p}", file=sys.stderr)
        return 2
    schema = sorted((info or {}).get("payload_schema") or {})
    print(f"verify : OK dense={dim}/Cosine sparse=idf payload_indexes={len(schema)}")
    print(f"         {schema}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
