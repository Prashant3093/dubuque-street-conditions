# Reproducing the condition map in QGIS (and, optionally, ArcGIS Online)

The Python pipeline already produces `outputs/figures/condition_map_2021.png`. This guide rebuilds the same map in a desktop GIS. It covers the Esri-style workflow: add data, join a table, symbolize, and lay out a printable map. It takes about 45 minutes the first time.

Before you start, run the pipeline once (see the README) so the files below exist:

| File | What it is |
|---|---|
| `data/processed/segments.geojson` | Rated street segments (line geometry, WGS84), keyed by `CenterlineID` |
| `data/processed/segment_attributes.csv` | One row per segment: rating for each survey year, band, deterioration rate, priority rank |
| `data/raw/road_centerlines.geojson` | All centerlines, used as gray context (re-created by `src/fetch_data.py`) |

## QGIS

### 1. Install (5 min)

```bash
brew install --cask qgis@ltr     # the Long Term Release; `qgis` gives the latest release
```

Alternatively, download the macOS installer from <https://qgis.org/download/>. On first launch, macOS may block the app: open **System Settings → Privacy & Security** and click **Open Anyway**.

### 2. Set up the project (3 min)

1. Open QGIS and choose **Project → New**.
2. In the bottom-right corner, click the CRS button and set the project CRS to **EPSG:26915 – NAD83 / UTM zone 15N**. This projection measures in meters, so the scale bar comes out correct.
3. Save the project as `gis/dubuque_condition.qgz` inside the repo.

### 3. Load the layers (5 min)

1. Choose **Layer → Add Layer → Add Vector Layer…**, pick `data/raw/road_centerlines.geojson`, and click **Add**. Rename the layer to *Context roads*.
2. Repeat for `data/processed/segments.geojson` and rename it *Rated segments*.
3. Choose **Layer → Add Layer → Add Delimited Text Layer…** and pick `data/processed/segment_attributes.csv`.
   - File format: **CSV**.
   - Geometry definition: **No geometry (attribute only table)**.
   - Under *Record and Fields Options*, tick **Detect field types**. This makes `CenterlineID` an integer, which the join needs.
   - Click **Add**.

### 4. Join the CSV to the lines (5 min)

1. Right-click *Rated segments* and choose **Properties → Joins**, then click the green **+**.
2. Set these options:
   - Join layer: `segment_attributes`
   - Join field: `CenterlineID`
   - Target field: `CenterlineID`
3. Expand **Joined fields** and tick `r2009 … r2021`, `deterioration_rate` and `priority_rank`. Set **Custom field name prefix** to `a_`.
4. Click **OK**. To confirm the join worked, right-click the layer and choose **Open Attribute Table**. The `a_` columns should be filled.
   - If they're blank, the key types don't match. Re-add the CSV with *Detect field types* ticked.

### 5. Symbolize (10 min)

1. For *Context roads*, open **Properties → Symbology** and choose a **Single symbol** line in color `#dcdbd5`, width 0.2 mm.
2. For *Rated segments*, open **Properties → Symbology** and choose **Graduated**.
   - Value: `a_r2021`. You could use `rating_2021`, which is already on the GeoJSON, but using the joined field shows the join did its job.
   - Mode: **Fixed interval**. Then edit the classes by hand: double-click each range and set the values, colors and legend text from this table:

     | Lower | Upper | Color | Legend |
     |---|---|---|---|
     | 71 | 100 | `#256abf` | Good (71–100) |
     | 56 | 70 | `#86b6ef` | Fair (56–70) |
     | 26 | 55 | `#e87471` | Poor (26–55) |
     | 1 | 25 | `#8a1f1f` | Failed (1–25) |

   - Optionally, make line width data-defined so arterials draw thicker. Click the ε icon next to *Width* and enter:
     `CASE WHEN "cl_DOT_FClassification" IN ('MinorArterial', 'OtherPrincipalArterial') THEN 0.8 WHEN "cl_DOT_FClassification" IN ('MajorCollector', 'MinorCollector') THEN 0.55 ELSE 0.35 END`
   - A simpler alternative is to keep the default 0.45 mm.
3. In **Layer Rendering → Symbol levels**, enable symbol levels and put *Failed* and *Poor* on top, so the poor streets aren't hidden under good ones.

### 6. Print layout and PDF export (15 min)

1. Choose **Project → New Print Layout…** and name it `Condition 2021`. Set the page to A4 or Letter, landscape.
2. Add the map: choose **Add Item → Add Map** and drag a box over most of the page. In *Item Properties*, set the scale to about **1:40,000** so it frames the City.
3. Add the legend: choose **Add Item → Add Legend**. Untick **Auto update** and remove `segment_attributes`. Rename the legend title to *2021 rating band*.
4. Add the scale bar: choose **Add Item → Add Scale Bar**. Set the style to *Single Box*, units to *Miles*, and 1 segment of 1 mile.
5. Add the north arrow: choose **Add Item → Add North Arrow**.
6. Add the title: choose **Add Item → Add Label** and enter *Dubuque street condition, 2021*.
7. Add the attribution: add another label with this text:
   *"Data: City of Dubuque Engineering and Public Works Departments, GIS Office. Independent analysis; not affiliated with or endorsed by the City."*
8. Export: choose **Layout → Export as PDF…** and save to `outputs/figures/condition_map_2021_qgis.pdf`.

Save the project (`.qgz`). Committing it is fine; it stores relative paths if you saved it inside the repo.

## ArcGIS Online (optional)

What a free **ArcGIS public account** adds:

- A shareable web map in the City's own ecosystem. Esri is what the Public Works department uses.
- Direct access to the City's live service alongside your own layers.

Its main limitation is that a public account can only add files of **fewer than 1,000 features** directly to a web map ([Esri support](https://support.esri.com/en-us/knowledge-base/what-are-the-options-to-display-a-shapefile-with-over-1-000012614)). The full 3,069-segment layer is too large, so do this:

1. Create a public account at <https://www.arcgis.com> (**Sign In → Create a public account**).
2. Open **Map Viewer → Add → Add layer from URL** and paste the City's own service:
   `https://maps.cityofdubuque.org/arcgisweb/rest/services/Street_Ratings_for_Web/MapServer/2`
   This shows the City's full network, styled live.
3. Choose **Add → Add layer from file** and upload `data/processed/segments_poor_2021.geojson`, which has 298 features and stays under the limit. Style it by `band_2021` in red, or by `priority_rank`.
4. Configure pop-ups to show `StreetName`, `FromStreet`, `ToStreet`, `rating_2021` and `deterioration_rate`.
5. **Save** the map, then **Share → Everyone**, and link it from the README.

To host the **full** layer as a hosted feature layer, you need an organizational account or Esri's developer/Location Platform free tier. Check Esri's current terms before relying on either, because the free-tier limits change. That's optional for this project: the QGIS PDF and the Leaflet map already cover the deliverable.
