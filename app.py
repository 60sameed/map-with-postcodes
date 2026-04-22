from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bcrypt
import pandas as pd
import pydeck as pdk
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
MAP_HEIGHT = 700
VIEWPORT_OFFSET = 120
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
            st.query_params["auth"] = "1"
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


def build_view_state(dataframe: pd.DataFrame, selected_row: dict[str, Any] | None) -> pdk.ViewState:
    if selected_row is not None:
        return pdk.ViewState(
            latitude=float(selected_row["latitude"]),
            longitude=float(selected_row["longitude"]),
            zoom=10.5,
            pitch=0,
        )

    if dataframe.empty:
        return pdk.ViewState(latitude=54.5, longitude=-3.0, zoom=5.1, pitch=0)

    latitudes = dataframe["latitude"].astype(float)
    longitudes = dataframe["longitude"].astype(float)
    lat_span = max(latitudes.max() - latitudes.min(), 0.15)
    lon_span = max(longitudes.max() - longitudes.min(), 0.15)
    span = max(lat_span, lon_span)
    zoom = max(5.0, min(10.0, 7.8 - math.log2(span * 7)))

    return pdk.ViewState(
        latitude=float(latitudes.mean()),
        longitude=float(longitudes.mean()),
        zoom=float(zoom),
        pitch=0,
    )


def build_map(dataframe: pd.DataFrame, selected_row: dict[str, Any] | None = None) -> pdk.Deck:
    view_state = build_view_state(dataframe, selected_row)

    marker_layer = pdk.Layer(
        "ScatterplotLayer",
        data=dataframe,
        get_position="[longitude, latitude]",
        get_radius=10,
        get_fill_color=[200, 30, 30, 90],
        radius_units="pixels",
        pickable=True,
    )
    text_layer = pdk.Layer(
        "TextLayer",
        data=dataframe,
        get_position="[longitude, latitude]",
        get_text="name",
        get_size=14,
        get_color=[20, 20, 20, 255],
        get_alignment_baseline="'top'",
        get_pixel_offset=[0, 14],
    )
    layers: list[pdk.Layer] = [marker_layer, text_layer]

    if selected_row is not None:
        selected_data = [selected_row]
        highlight_ring = pdk.Layer(
            "ScatterplotLayer",
            data=selected_data,
            get_position="[longitude, latitude]",
            get_radius=22,
            get_fill_color=[255, 196, 0, 80],
            get_line_color=[255, 160, 0, 255],
            line_width_min_pixels=3,
            radius_units="pixels",
            stroked=True,
            filled=True,
            pickable=True,
        )
        highlight_dot = pdk.Layer(
            "ScatterplotLayer",
            data=selected_data,
            get_position="[longitude, latitude]",
            get_radius=14,
            get_fill_color=[255, 140, 0, 140],
            radius_units="pixels",
            pickable=True,
        )
        highlight_label = pdk.Layer(
            "TextLayer",
            data=selected_data,
            get_position="[longitude, latitude]",
            get_text="name",
            get_size=18,
            get_color=[0, 0, 0, 255],
            get_alignment_baseline="'bottom'",
            get_pixel_offset=[0, -18],
        )
        layers.extend([highlight_ring, highlight_dot, highlight_label])

    return pdk.Deck(
        map_style="light",
        initial_view_state=view_state,
        layers=layers,
        tooltip={"text": "{name}\n{postcode}"},
        height=MAP_HEIGHT,
    )


def render_map(dataframe: pd.DataFrame, selected_row: dict[str, Any] | None = None) -> None:
    deck = build_map(dataframe, selected_row=selected_row)
    deck_html = deck.to_html(as_string=True, iframe_width="100%", iframe_height="100%")
    deck_html = deck_html.replace("const deckInstance = createDeck(", "window.deckInstance = createDeck(")
    resize_script = """
    <script>
    const resizeFrameToViewport = () => {
      if (!window.frameElement) return;
      const rect = window.frameElement.getBoundingClientRect();
      const available = Math.max(window.parent.innerHeight - rect.top - 12, 320);
      window.frameElement.style.height = `${available}px`;
      const innerFrame = document.querySelector("iframe");
      if (innerFrame) {
        innerFrame.style.height = "100%";
      }
      document.documentElement.style.height = "100%";
      document.body.style.height = "100%";
    };
    resizeFrameToViewport();
    window.addEventListener("load", resizeFrameToViewport);
    window.addEventListener("resize", resizeFrameToViewport);
    </script>
    """
    fullscreen_ui = """
    <style>
    .map-shell {
      position: relative;
      width: 100%;
      height: 100%;
    }
    .map-fullscreen-btn {
      position: absolute;
      top: 12px;
      right: 12px;
      z-index: 9999;
      border: 1px solid rgba(15, 23, 42, 0.12);
      background: rgba(255, 255, 255, 0.94);
      color: #0f172a;
      border-radius: 10px;
      padding: 8px 12px;
      font: 600 13px/1 sans-serif;
      cursor: pointer;
      box-shadow: 0 10px 24px rgba(15, 23, 42, 0.12);
    }
    .map-fullscreen-btn:hover {
      background: #ffffff;
    }
    .map-zoom-controls {
      position: absolute;
      top: 60px;
      right: 12px;
      z-index: 9999;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .map-zoom-btn {
      width: 40px;
      height: 40px;
      border: 1px solid rgba(15, 23, 42, 0.12);
      background: rgba(255, 255, 255, 0.94);
      color: #0f172a;
      border-radius: 10px;
      font: 700 22px/1 sans-serif;
      cursor: pointer;
      box-shadow: 0 10px 24px rgba(15, 23, 42, 0.12);
    }
    .map-zoom-btn:hover {
      background: #ffffff;
    }
    </style>
    <script>
    const getMapHandle = () => window.deckInstance?.map || window.deckInstance?._map || null;
    const getDeckHandle = () => window.deckInstance?.deck || window.deckInstance || null;
    const stepZoom = (delta) => {
      const map = getMapHandle();
      if (map && typeof map.getZoom === "function" && typeof map.easeTo === "function") {
        map.easeTo({ zoom: map.getZoom() + delta, duration: 250 });
        return;
      }
      const deck = getDeckHandle();
      const current = deck?.props?.initialViewState || deck?.viewState;
      if (deck && current && typeof deck.setProps === "function") {
        deck.setProps({
          initialViewState: {
            ...current,
            zoom: (current.zoom || 0) + delta
          }
        });
      }
    };
    const toggleFullscreen = async () => {
      const host = window.frameElement;
      if (!host) return;
      if (document.fullscreenElement || window.parent.document.fullscreenElement) {
        try {
          await (window.parent.document.exitFullscreen?.() || document.exitFullscreen());
        } catch (error) {
          console.error(error);
        }
        return;
      }
      try {
        await host.requestFullscreen();
      } catch (error) {
        console.error(error);
      }
    };
    window.addEventListener("load", () => {
      const button = document.createElement("button");
      button.className = "map-fullscreen-btn";
      button.type = "button";
      button.textContent = "Full screen";
      button.onclick = toggleFullscreen;
      document.body.appendChild(button);

      const controls = document.createElement("div");
      controls.className = "map-zoom-controls";

      const zoomIn = document.createElement("button");
      zoomIn.className = "map-zoom-btn";
      zoomIn.type = "button";
      zoomIn.textContent = "+";
      zoomIn.onclick = () => stepZoom(1);

      const zoomOut = document.createElement("button");
      zoomOut.className = "map-zoom-btn";
      zoomOut.type = "button";
      zoomOut.textContent = "−";
      zoomOut.onclick = () => stepZoom(-1);

      controls.appendChild(zoomIn);
      controls.appendChild(zoomOut);
      document.body.appendChild(controls);
    });
    </script>
    """
    deck_html = deck_html.replace("</body>", f"{resize_script}{fullscreen_ui}</body>")
    components.html(
        deck_html,
        height=MAP_HEIGHT,
        scrolling=False,
    )


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
    if st.query_params.get("auth", "") == "1":
        st.session_state.authenticated = True
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
        render_map(pd.DataFrame(columns=["name", "postcode", "latitude", "longitude"]))
        return

    if not source_label:
        source_label = f"Loaded saved CSV from {SAVED_CSV_PATH.name}."
    st.caption(source_label)

    try:
        normalised = normalise_csv(dataframe)
        enriched, missing_postcodes = enrich_with_coordinates(normalised)
        records = enriched.to_dict(orient="records")
        save_json(records)
    except requests.RequestException as error:
        st.error(f"Postcode lookup failed. The app could not reach postcodes.io. Details: {error}")
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
        render_map(enriched, selected_row=selected_row)


if __name__ == "__main__":
    main()
