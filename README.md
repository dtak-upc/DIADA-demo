# Demo for DIADA

In this repo we deomnstrate DIADA in a pipeline to automatically compose datasets. Starting from an unorganized data lake, this tool creates a curated repository where data attributes are composed based on statistical dependencies. Given a folder of CSVs, it
profiles every dataset, discovers likely join keys across them, and either lets you build/curate a join by hand or has it do the whole lake autonomously.

## What's here

- `backend/` — FastAPI service (Python). Profiling, join discovery, join
  building, and the two autonomous gold-layer algorithms.
- `frontend/` — React (Vite) UI.
- `backend/tools/DIADA-0.8.jar` — the DIADA jar (vendored from
  `arclo/benchmarks_generation`), invoked as a subprocess. Needs a Java
  11+ runtime.
- `backend/models/` — a small pre-trained model used to rank join
  candidates.
- `urban_join_demo/` — a synthetic 5-file demo lake, purpose-built so
  every feature below has something real to find (see "Demo dataset"
  below).
- `Dockerfile`, `docker-compose.yml` — single-container deployment.

## Running it

### Docker (recommended)

Only prerequisite is Docker itself.

```bash
docker compose up --build
```

Open **http://localhost:8000**. The image bundles `urban_join_demo/`'s
CSVs, so when accessing the URL you will immediately see some datasets and their properties.

- **Persistence**: everything the app computes is kept in the `diada_data`
  named volume, so it survives `docker compose down`/`up` (not
  `down -v`, which wipes it).
- **Using your own data**: a project's folder path is read by the backend
  process directly, not uploaded. You can load your own data by clicking the "Projects" button on the top right of the screen.
  path.

### Local development

Requires Python 3.11, Node 20, and a Java 11+ runtime on `PATH`.

```bash
conda env create -f environment.yml   # or: pip install -r backend/requirements.txt
conda activate diada-demo
cd frontend && npm install && cd ..
python run.py
```

Open http://localhost:5173 (proxies `/api` to the backend on :8000).
`python run.py --backend-only` / `--frontend-only` run just one side;
`--backend-port`/`--frontend-port` change ports.

## The main parts

**Projects.** By clicking the "Projects" button on the top right you can cehck which projects have been created and generate a new one. Doing so asks you to introduce a name for the project and a path to the data's directory. Every `*.csv` in it becomes
a dataset. Multiple projects can exist, and one is *active* at a time. Everything computed is
persisted under `.diada/` (`DIADA_DIR` in `backend/app/core/config.py`),
keyed per project, so re-opening a project is a disk read (i.e. nothing is recomputed).

**Datasets tab.** Once data has been ingested this tab showcases, per dataset, its schema and data-quality flags per column (computed at ingestion time): nulls, top-value dominance, outliers, unusable types. A deep statistical profile per column is also included: cardinality, distribution, common values, etc. 

**Catalog graph tab.** This tab showcases a metadata graph of the project, where all the relevant information can be seen. With many datasets, this view becomes to clogged to see anything, so its purpose is to demonstrate that this structure is actually being computed, although it is used primarily internally. It includes a search bar to look for specific datasets.

**Manual composition tab.** Here you can perform manual data compositions, that is, to go over the main steps require to compose data. First, you need to pick a base dataset and columns, which will allow you to find joins for such column and rank every other usable column by predicted joinability (a pre-trained gradient boosting model over profile-distance features). From the results you can build a multi-way join that handles aggregation for non-unique keys and name collisions. The resut can be optionally decluttered of columns that add no value (unused primary keys, redundant column groups, mostly-null columns). Finally, the datasets can be composed with DIADA. There are two versions of this. First, the system can "remove univariate noise" that is not related to any other variables and, second,  "structure dataset", which actually composes the joined data into several components of mutually related attributes.

**Automatic composition tab.** Same underlying machinery as in the previous tab, but this time fully automated. That is, the entire data lake is composed autonomously, without requiring human intervention. Two independent algorithms, each with its own config and persisted result:
- **Every table as base**: every table is used as a base table and joins are found only for it. Akin to enriching a given table.
- **Ad hoc**: true traversal of the join space that is created, combining assests until a maximum number of columns is reached and then composing the data.

Key parameters (both algorithms, adjustable in the UI):
| Parameter | Default | What it does |
|---|---|---|
| Discovery score threshold | `0.001` | Minimum joinability score to try a candidate. This is deliberately permissive as DIADA's curation cleans up a bad candidate downstream better than a strict threshold risks silently missing a real one. |
| Candidate uniqueness threshold | `0.9` | A candidate column must be this unique in its own dataset to be joined *through* (not required of the base column). |
| Max columns per asset (ad hoc only) | `80` | Growth stops once an asset would exceed this. |
| Parallel workers | `4`–`8` | Bases/assets processed concurrently; bottlenecked on the DIADA subprocess, not CPU. |

Every final table records exactly how it was built, what got decluttered and what DIADA dropped, shown
in each table's expanded row in the UI.

## Demo dataset: urban_join_demo

This small benchmark of five CSVs (`property_listings` as the base, plus `district_properties`,
`weather_stations`, `permit_applications`, `resident_reviews`), has been manually built to showcase the purpose of the system. The result of the automatic composition should showcase a dataset completely separated (`permit_applications`), whereas the rest are divided into 2 clusters (one combining attributes from `district_properties`, `resident_reviews` and `property_listings`, and the other an isolated cluster with the attributes from weather_stations) + another grouped for unrelated data. 

```bash
python urban_join_demo/generate_urban_demo_data.py
```

See that script's own module docstring for the full design and exact tuning of each relationship.

## API

All endpoints are under `/api`; see `backend/app/main.py`'s own module
docstring for the full list with descriptions. The frontend's
`frontend/src/api/client.js` is a 1:1 mirror if you want the exact request/
response shapes.
