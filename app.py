from __future__ import annotations

import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bcrypt
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components


BASE_DIR = Path(__file__).parent
AUTH_CONFIG_PATH = BASE_DIR / "auth_config.json"
DATA_DIR = BASE_DIR / "data"
OUTPUT_JSON_PATH = DATA_DIR / "uploaded_postcodes.json"
SAVED_CSV_PATH = DATA_DIR / "uploaded_postcodes.csv"
POSTCODES_API_URL = "https://api.postcodes.io/postcodes"
CSV_COLUMNS = ["name", "postcode"]
MAP_HEIGHT = 920
VIEWPORT_OFFSET = 140
TABLE_HEIGHT = 520


def load_auth_hash() -> str:
    config = json.loads(AUTH_CONFIG_PATH.read_text(encoding="utf-8"))
    return config["password_hash"]


def is_password_valid(password: str) -> bool:
    stored_hash = load_auth_hash().encode("utf-8")
    return bcrypt.checkpw(password.encode("utf-8"), stored_hash)


def show_login() -> None:
    st.title("UK Postcode Mapper")
    st.subheader("Sign in")
    with st.form("login_form", clear_on_submit=False):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Unlock", type="primary")
    if submitted:
        if is_password_valid(password):
            st.session_state.authenticated = True
            st.rerun()
        st.error("Incorrect password.")
    st.stop()


def normalise_csv(dataframe: pd.DataFrame) -> pd.DataFrame:
    dataframe.columns = [str(column).strip().lower() for column in dataframe.columns]
    if dataframe.columns.tolist() != CSV_COLUMNS:
        raise ValueError("CSV must contain exactly two columns named 'name' and 'postcode'.")

    cleaned = dataframe.copy()
    cleaned["name"] = cleaned["name"].astype(str).str.strip()
    cleaned["postcode"] = cleaned["postcode"].astype(str).str.strip().str.upper()
    cleaned = cleaned[(cleaned["name"] != "") & (cleaned["postcode"] != "")]

    if cleaned.empty:
        raise ValueError("The uploaded CSV does not contain any usable rows.")

    return cleaned


@st.cache_data(show_spinner=False)
def lookup_postcodes(postcodes: tuple[str, ...]) -> dict[str, dict[str, Any] | None]:
    response = requests.post(
        POSTCODES_API_URL,
        json={"postcodes": list(postcodes)},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()["result"]

    lookup: dict[str, dict[str, Any] | None] = {}
    for item in payload:
        query = str(item["query"]).strip().upper()
        result = item.get("result")
        lookup[query] = result
    return lookup


def enrich_with_coordinates(dataframe: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    unique_postcodes = tuple(dict.fromkeys(dataframe["postcode"].tolist()))
    lookup = lookup_postcodes(unique_postcodes)

    enriched = dataframe.copy()
    enriched["lookup_result"] = enriched["postcode"].map(lookup)
    missing = sorted(
        {
            postcode
            for postcode, result in zip(enriched["postcode"], enriched["lookup_result"])
            if not result
        }
    )
    enriched = enriched[enriched["lookup_result"].notna()].copy()

    if enriched.empty:
        raise ValueError("No valid UK postcodes were resolved from the uploaded CSV.")

    enriched["latitude"] = enriched["lookup_result"].apply(lambda item: item["latitude"])
    enriched["longitude"] = enriched["lookup_result"].apply(lambda item: item["longitude"])
    enriched = enriched.drop(columns=["lookup_result"])

    return enriched, missing


def save_json(records: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records": records,
    }
    OUTPUT_JSON_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def save_uploaded_csv(file_bytes: bytes) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SAVED_CSV_PATH.write_bytes(file_bytes)


def load_saved_csv() -> pd.DataFrame | None:
    if not SAVED_CSV_PATH.exists():
        return None
    return pd.read_csv(SAVED_CSV_PATH)


def inject_styles() -> None:
    components.html(
        """
        <script>
        const syncViewportHeight = () => {
          const appHeight = `${window.innerHeight}px`;
          window.parent.document.documentElement.style.setProperty("--app-height", appHeight);
        };
        syncViewportHeight();
        window.addEventListener("resize", syncViewportHeight);
        </script>
        """,
        height=0,
        width=0,
    )
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 2rem;
            padding-left: 2rem;
            padding-right: 2rem;
            max-width: 100%;
        }
        div[data-testid="stHorizontalBlock"] {
            align-items: stretch;
        }
        div[data-testid="stHorizontalBlock"] > div:first-child {
            min-width: 0;
        }
        div[data-testid="column"]:first-child > div[data-testid="stVerticalBlock"] {
            height: calc(var(--app-height, 100vh) - __VIEWPORT_OFFSET__px);
        }
        div[data-testid="column"]:first-child div[data-testid="stDeckGlJsonChart"] {
            height: 100% !important;
        }
        div[data-testid="column"]:first-child div[data-testid="stDeckGlJsonChart"] > div {
            height: 100% !important;
        }
        div[data-testid="column"]:first-child iframe,
        div[data-testid="column"]:first-child canvas {
            height: 100% !important;
        }
        div[data-testid="column"]:nth-child(2) > div[data-testid="stVerticalBlock"] {
            height: calc(var(--app-height, 100vh) - __VIEWPORT_OFFSET__px);
            overflow-y: auto;
            overflow-x: hidden;
            padding-right: 0.35rem;
        }
        </style>
        """.replace("__VIEWPORT_OFFSET__", str(VIEWPORT_OFFSET)),
        unsafe_allow_html=True,
    )


def build_map_html(dataframe: pd.DataFrame, selected_row: dict[str, Any] | None = None) -> str:
    records = [
        {
            "name": str(row["name"]),
            "postcode": str(row["postcode"]),
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
        }
        for _, row in dataframe.iterrows()
    ]
    selected_payload = None
    if selected_row is not None:
        selected_payload = {
            "name": str(selected_row["name"]),
            "postcode": str(selected_row["postcode"]),
            "latitude": float(selected_row["latitude"]),
            "longitude": float(selected_row["longitude"]),
        }

    no_data = dataframe.empty
    title = html.escape("UK Postcode Mapper")

    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      <link
        rel="stylesheet"
        href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
        integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
        crossorigin=""
      />
      <link
        rel="stylesheet"
        href="https://unpkg.com/leaflet.fullscreen@3.0.0/Control.FullScreen.css"
      />
      <style>
        html, body, #map {{
          margin: 0;
          width: 100%;
          height: 100%;
          font-family: sans-serif;
        }}
        .postcode-label {{
          background: transparent;
          border: none;
          box-shadow: none;
          color: #111;
          font-size: 14px;
          font-weight: 600;
        }}
        .empty-state {{
          display: flex;
          align-items: center;
          justify-content: center;
          height: 100%;
          color: #555;
          background: #f5f7fb;
        }}
      </style>
    </head>
    <body>
      <div id="map"></div>
      <script
        src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
        crossorigin=""
      ></script>
      <script src="https://unpkg.com/leaflet.fullscreen@3.0.0/Control.FullScreen.js"></script>
      <script>
        const records = {json.dumps(records)};
        const selected = {json.dumps(selected_payload)};

        if ({str(no_data).lower()}) {{
          document.getElementById("map").outerHTML = '<div class="empty-state">{title}</div>';
        }} else {{
          const map = L.map("map", {{
            zoomControl: true,
            fullscreenControl: true,
            fullscreenControlOptions: {{
              position: "topleft",
              title: "Full screen",
              titleCancel: "Exit full screen"
            }}
          }});
          L.tileLayer("https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png", {{
            attribution: '&copy; CARTO, OpenStreetMap contributors',
            subdomains: 'abcd',
            maxZoom: 20
          }}).addTo(map);

          const bounds = [];
          records.forEach((record) => {{
            const latlng = [record.latitude, record.longitude];
            bounds.push(latlng);
            const marker = L.circleMarker(latlng, {{
              radius: 8,
              color: "#c81e1e",
              weight: 1,
              fillColor: "#d62f2f",
              fillOpacity: 0.85
            }}).addTo(map);
            marker.bindTooltip(
              `${{record.name}}`,
              {{
                permanent: true,
                direction: "bottom",
                offset: [0, 12],
                className: "postcode-label"
              }}
            );
            marker.bindPopup(`<strong>${{record.name}}</strong><br/>${{record.postcode}}`);
          }});

          if (selected) {{
            const focusLatLng = [selected.latitude, selected.longitude];
            L.circle(focusLatLng, {{
              radius: 18000,
              color: "#ff9f0a",
              weight: 3,
              fillColor: "#ffc107",
              fillOpacity: 0.2
            }}).addTo(map);
            L.circleMarker(focusLatLng, {{
              radius: 10,
              color: "#ff6a00",
              weight: 2,
              fillColor: "#ff8f1f",
              fillOpacity: 0.95
            }})
              .addTo(map)
              .bindTooltip(
                `${{selected.name}}`,
                {{
                  permanent: true,
                  direction: "top",
                  offset: [0, -10],
                  className: "postcode-label"
                }}
              );
            map.setView(focusLatLng, 10);
          }} else if (bounds.length === 1) {{
            map.setView(bounds[0], 10);
          }} else {{
            map.fitBounds(bounds, {{ padding: [40, 40] }});
          }}
        }}
      </script>
    </body>
    </html>
    """


def build_selection_table(dataframe: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any] | None]:
    table = dataframe.reset_index(drop=True).copy()
    table.insert(0, "focus", False)

    previous_index = st.session_state.get("selected_row_index")
    if previous_index is not None and 0 <= previous_index < len(table):
        table.at[previous_index, "focus"] = True

    edited = st.data_editor(
        table,
        use_container_width=True,
        hide_index=True,
        height=TABLE_HEIGHT,
        disabled=["name", "postcode", "latitude", "longitude"],
        column_config={
            "focus": st.column_config.CheckboxColumn("Focus", help="Tick a row to highlight it on the map."),
            "latitude": st.column_config.NumberColumn("latitude", format="%.4f"),
            "longitude": st.column_config.NumberColumn("longitude", format="%.4f"),
        },
        key="selection_table",
    )

    selected_indexes = edited.index[edited["focus"]].tolist()
    if not selected_indexes:
        if previous_index is not None:
            st.session_state.selected_row_index = None
            st.session_state.pop("selection_table", None)
            st.rerun()
        st.session_state.selected_row_index = None
        return edited, None

    selected_index = selected_indexes[-1]

    if len(selected_indexes) > 1 or selected_index != previous_index:
        st.session_state.selected_row_index = selected_index
        st.session_state.pop("selection_table", None)
        st.rerun()

    st.session_state.selected_row_index = selected_index

    return edited, edited.loc[selected_index].drop(labels=["focus"]).to_dict()


def main() -> None:
    st.set_page_config(page_title="UK Postcode Mapper", layout="wide")
    st.session_state.setdefault("authenticated", False)
    inject_styles()

    if not st.session_state.authenticated:
        show_login()

    st.title("UK Postcode Mapper")
    st.write("Upload a CSV with exactly two columns: `name` and `postcode`.")
    uploaded_file = st.file_uploader("Upload CSV", type=["csv"])

    source_label = ""
    if uploaded_file is not None:
        uploaded_bytes = uploaded_file.getvalue()
        save_uploaded_csv(uploaded_bytes)
        dataframe = pd.read_csv(SAVED_CSV_PATH)
        source_label = f"Loaded uploaded CSV and saved it as {SAVED_CSV_PATH.name}."
    else:
        dataframe = load_saved_csv()

    if dataframe is None:
        st.info("Upload a CSV file to generate JSON output and map the postcodes.")
        components.html(
            build_map_html(pd.DataFrame(columns=["name", "postcode", "latitude", "longitude"])),
            height=MAP_HEIGHT,
            scrolling=False,
        )
        return

    if not source_label:
        source_label = f"Loaded saved CSV from {SAVED_CSV_PATH.name}."
    st.caption(source_label)

    try:
        normalised = normalise_csv(dataframe)
        enriched, missing_postcodes = enrich_with_coordinates(normalised)
        records = enriched.to_dict(orient="records")
        save_json(records)
    except requests.RequestException:
        st.error("Postcode lookup failed. Check the network connection and try again.")
        return
    except ValueError as error:
        st.error(str(error))
        return

    left_col, right_col = st.columns([5, 1.7], gap="medium")
    with right_col:
        _, selected_row = build_selection_table(enriched)
        st.success(f"Saved JSON to {OUTPUT_JSON_PATH.name}")
        st.metric("Mapped rows", len(enriched))
        st.metric("Unique postcodes", enriched["postcode"].nunique())
        if selected_row is not None:
            st.info(f"Focused marker: {selected_row['name']} ({selected_row['postcode']})")
        if missing_postcodes:
            st.warning("Unresolved postcodes: " + ", ".join(missing_postcodes))
        st.caption("Tick a row in the table to focus and highlight that marker.")
    with left_col:
        components.html(
            build_map_html(enriched, selected_row=selected_row),
            height=MAP_HEIGHT,
            scrolling=False,
        )


if __name__ == "__main__":
    main()
