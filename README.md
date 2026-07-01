# Ynet crawler

This repository contains a small, dependency-free Python crawler for Ynet.

The crawler:

- Fetches one or more Ynet section pages.
- Discovers article URLs.
- Fetches a bounded number of article pages.
- Extracts title, description, publication time, section, and a text excerpt.
- Writes the result as JSON.

## Usage

```bash
python3 ynet_crawler.py --limit 10 --pretty --output ynet_articles.json
```

You can target a specific section with `--start-url`:

```bash
python3 ynet_crawler.py \
  --start-url https://www.ynet.co.il/news \
  --limit 5 \
  --pretty
```

## Tests

```bash
python3 -m unittest -v
```
