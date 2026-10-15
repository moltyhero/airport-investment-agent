"""One-time (resumable) download of the BTS On-Time Performance rolling 12-month window: python -m airport_agent.build_cache

~12 monthly files of ~30 MB each; each is reduced to a small per-airport JSON in .cache/ and the zip is deleted.
"""
from . import data_sources as ds

if __name__ == "__main__":
    ds.build_bts_cache()
    print(f"Done: {ds.bts_cached_months()} months cached for {ds.BTS_LABEL}.")
