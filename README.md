# Brokerage Brief

A news site for the global brokerage industry: equities, FX & CFD, wealth management and digital assets.
It reads trade-press and regulator RSS feeds, files each story under a section, and builds a static site.

## Sections

| Section | What lands there |
|---|---|
| People Moves | Hires, appointments, promotions, departures |
| Technology & Infrastructure | Platforms, liquidity and connectivity, market data, cloud, AI, outages, vendors |
| Regulation & Compliance | Licences, fines, enforcement, rule changes |
| Deals & Corporate | M&A, funding, results, volumes, expansion |
| Wealth Management | Advisers, RIAs, private banks, family offices, wealth platforms |
| Digital Assets | Crypto brokerage, tokenisation, stablecoins, prediction markets |
| Markets & Products | Launches, new instruments, everything else |

Every story is also tagged with asset class (Equities, FX & CFD, Wealth, Crypto) and region
(Americas, UK & Europe, Middle East & Africa, Asia Pacific, Global), which readers can filter on.
A story can appear in a second section when it fits both (for example a trading-tech vendor being acquired).

## How it works

- `feeds.json`: the list of sources. Add or pause a feed here. `region`/`asset` are fallbacks, `section` nudges a feed toward one section.
- `build.py`: fetches feeds, classifies stories with keyword rules, keeps 60 days of stories in `data/articles.json`, and writes `site/index.html`. Standard library only.
- `templates/index.html`: the page design. The build inlines the stories into it.
- `.github/workflows/update.yml`: runs the build every two hours and publishes to GitHub Pages.

## Run it locally

```
python3 build.py            # fetch feeds and build
python3 build.py --no-fetch # rebuild the page from stored stories
open site/index.html
```

## Tuning the sorting

Section rules live in `RULES` near the top of `build.py`. Each rule is a regex and a weight; a match in
the headline counts three times as much as one in the summary. Region and asset keywords are in `REGIONS` and `ASSETS`.

## Hosting on GitHub Pages

1. Create an empty GitHub repository and push this folder to `main`.
2. In the repo, open Settings → Pages and set Source to "GitHub Actions".
3. Open the Actions tab and run "Update news" once. The site is then live and refreshes every two hours.
