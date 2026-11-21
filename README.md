# Unlimited card feed

The card database behind Unlimited Damage, rebuilt every night by GitHub Actions and served by
GitHub Pages:

- `docs/swu_catalog.json`: every Star Wars: Unlimited set, card and variant. Built from
  [swu-db](https://www.swu-db.com). For recent sets, the official card database fills in whatever
  swu-db doesn't have yet.
- `docs/art/`: art for those filled-in cards only, mirrored once so the app never loads it from the
  official site. It's removed again once swu-db has the card.
- `docs/status.json`: when the last run happened and what it built.

The app downloads the catalog once a day. If the feed can't be reached, the app builds the catalog
straight from swu-db.

**Release day:** go to Actions → Build card feed → Run workflow, so you don't have to wait for the
nightly run.

**A set swu-db doesn't list at all:** add its release date to `catalog_rules.json` under
`releaseDates` (for example `"releaseDates": {"XYZ": "2027-03-13"}`). Until then the run log prints
`?? XYZ … add its release date`.

Don't edit `fetch_swu_catalog.py` or `catalog_rules.json` here. They're copied from the app's repo
by `tools/catalog_feed/publish.sh`.

Card text and images © Lucasfilm Ltd. & Fantasy Flight Games. Unofficial fan project.
