import streamlit as st
import os
import re
import sys
import tempfile
import subprocess
import textwrap
from PIL import Image
import pandas as pd

# ===================== Compatibility Patch =====================
if not hasattr(st, "rerun") and hasattr(st, "experimental_rerun"):
    st.rerun = st.experimental_rerun

# ===================== Constants =====================
CAPTIONS_FILE = "captions.csv"
LANDMARKS_FILE = "landmarks.csv"
IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff")

# ------------------ Native folder picker helper (subprocess) ------------------
# We'll create a temporary helper script that uses tkinter, run it as a subprocess,
# and read the chosen folder path from a temp file.
TMP_PICK_DIR = tempfile.gettempdir()
TMP_PICK_PATH = os.path.join(TMP_PICK_DIR, "st_folder_pick.txt")
TMP_PICK_SCRIPT = os.path.join(TMP_PICK_DIR, "st_folder_picker_helper.py")

PICKER_SCRIPT_CONTENT = textwrap.dedent(r'''
import tkinter as tk
from tkinter import filedialog
import sys, os
outpath = sys.argv[1] if len(sys.argv) > 1 else None
try:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title="Select the folder with images")
    root.destroy()
    if outpath:
        with open(outpath, "w", encoding="utf-8") as fh:
            fh.write(folder if folder else "")
except Exception:
    try:
        if outpath:
            with open(outpath, "w", encoding="utf-8") as fh:
                fh.write("")
    except Exception:
        pass
''')

def browse_folder_native_blocking():
    """
    Creates and runs a small helper script that opens a native folder dialog (tkinter)
    in a separate process and writes the chosen path into TMP_PICK_PATH.
    Returns the chosen path (string) or None if cancelled/failed.
    """
    # ensure old tmp is removed
    try:
        if os.path.exists(TMP_PICK_PATH):
            os.remove(TMP_PICK_PATH)
    except Exception:
        pass

    # write helper script
    try:
        with open(TMP_PICK_SCRIPT, "w", encoding="utf-8") as fh:
            fh.write(PICKER_SCRIPT_CONTENT)
    except Exception as e:
        st.error(f"Could not write helper script: {e}")
        return None

    # run helper script with the same python interpreter
    try:
        subprocess.run([sys.executable, TMP_PICK_SCRIPT, TMP_PICK_PATH])
    except Exception as e:
        st.error(f"Failed to launch folder picker helper: {e}")
        return None

    # read back chosen folder
    try:
        if os.path.exists(TMP_PICK_PATH):
            with open(TMP_PICK_PATH, "r", encoding="utf-8") as fh:
                s = fh.read().strip()
            return s if s else None
    except Exception as e:
        st.error(f"Failed to read chosen folder: {e}")
        return None

    return None

# Replace previous tkinter-based select_folder_dialog with subprocess-based wrapper
def select_folder_dialog():
    """
    Opens native folder dialog in a separate subprocess and returns the chosen folder
    path (or None if cancelled).
    """
    return browse_folder_native_blocking()

# ===================== Utilities =====================
def list_images(folder):
    return sorted([f for f in os.listdir(folder) if f.lower().endswith(IMG_EXTS)])

def pattern_for(shortname: str):
    # image_shortname_number.ext
    return re.compile(rf"^image_{re.escape(shortname)}_(\d+)\.[A-Za-z0-9]+$")

def validate_images(folder, shortname):
    pat = pattern_for(shortname)
    imgs = list_images(folder)
    valids, invalids = [], []
    for f in imgs:
        (valids if pat.match(f) else invalids).append(f)
    return valids, invalids, imgs

def highest_serial(valid_images):
    nums = []
    for name in valid_images:
        m = re.search(r"_(\d+)\.", name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 0

def rename_invalids_to_valid(folder, shortname, valid_images, invalid_images):
    next_id = highest_serial(valid_images) + 1
    renamed = []
    for fname in invalid_images:
        ext = os.path.splitext(fname)[1]
        new_name = f"image_{shortname}_{next_id}{ext}"
        os.rename(os.path.join(folder, fname), os.path.join(folder, new_name))
        renamed.append((fname, new_name))
        next_id += 1
    return renamed

def ensure_csv(path, cols):
    if not os.path.exists(path):
        pd.DataFrame(columns=cols).to_csv(path, index=False)
    return pd.read_csv(path)

def save_csv_atomic(df, path):
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)

def captions_map(df: pd.DataFrame):
    m = {}
    for _, r in df.iterrows():
        img = str(r["image_name"])
        cap = "" if pd.isna(r["caption"]) else str(r["caption"])
        m.setdefault(img, []).append(cap)
    return m

def landmarks_map(df: pd.DataFrame):
    m = {}
    for _, r in df.iterrows():
        img = str(r["image_name"])
        lm = "" if pd.isna(r["landmark"]) else str(r["landmark"])
        m[img] = lm
    return m

# ===================== State Init =====================
def init_state():
    st.session_state.setdefault("page", "select")
    st.session_state.setdefault("folder", None)
    st.session_state.setdefault("shortname", "")
    st.session_state.setdefault("valid", [])
    st.session_state.setdefault("invalid", [])
    st.session_state.setdefault("all_imgs", [])
    st.session_state.setdefault("caption_index", 0)
    st.session_state.setdefault("confirm_caption_anyway", False)
    st.session_state.setdefault("validate_message", "")  # persistent success/info after renaming

    # Filters
    st.session_state.setdefault("cap_min", 0)
    st.session_state.setdefault("cap_max", 10)
    st.session_state.setdefault("landmark_filter", "Any")
    st.session_state.setdefault("filter_applied", False)

# ===================== Page 1: Folder Selection (centered) =====================
def page_select_folder():
    st.set_page_config(page_title="Image Tool", layout="centered")
    left, center, right = st.columns([1, 2, 1])
    with center:
        st.markdown("<h2 style='text-align:center;'>Select the folder with images</h2>", unsafe_allow_html=True)
        if st.button("📂 Select Folder", use_container_width=True):
            folder = select_folder_dialog()
            if folder:
                st.session_state.folder = folder
                st.session_state.page = "validate"
                st.rerun()

# ===================== Page 2: Validation =====================
def page_validation():
    st.set_page_config(page_title="Image Validation", layout="wide")
    folder = st.session_state.folder
    if not folder:
        st.session_state.page = "select"
        st.rerun()

    st.markdown("### Image validation section")
    st.write(f"**Current folder:** `{folder}`")

    # Back button always visible
    if st.button("🔙 Back to folder selection"):
        st.session_state.page = "select"
        st.rerun()

    # Show persistent validation message (if any)
    if st.session_state.validate_message:
        st.success(st.session_state.validate_message)

    imgs = list_images(folder)
    st.info(f"Total images found: **{len(imgs)}**")

    shortname = st.text_input(
        "Enter short name to validate (format: image_shortname_number):",
        value=st.session_state.shortname
    )

    if not shortname.strip():
        st.info("Enter your short name to validate images.")
        return

    # Validate & store
    valids, invalids, all_imgs = validate_images(folder, shortname)
    st.session_state.shortname = shortname
    st.session_state.valid = valids
    st.session_state.invalid = invalids
    st.session_state.all_imgs = all_imgs

    c1, c2, c3 = st.columns(3)
    c1.success(f"Valid images: **{len(valids)}**")
    c2.error(f"Invalid images: **{len(invalids)}**")
    c3.info(f"Total: **{len(all_imgs)}**")

    with st.expander("Show valid images"):
        for v in valids:
            st.write("•", v)
    with st.expander("Show invalid images"):
        if invalids:
            for iv in invalids:
                st.write("•", iv)
        else:
            st.write("None")

    a1, a2 = st.columns(2)
    with a1:
        if st.button("🧩 Validate all invalid images (rename)"):
            if invalids:
                renamed = rename_invalids_to_valid(folder, shortname, valids, invalids)
                # set persistent message, then rerun to refresh lists
                st.session_state.validate_message = f"Renamed {len(renamed)} images successfully."
                st.rerun()
            else:
                st.session_state.validate_message = "No invalid images to rename."
                st.rerun()

    with a2:
        if st.button("➡️ Go to captioning"):
            if len(st.session_state.invalid) > 0:
                st.warning("Some images are invalid. Proceeding will show **only valid images**.")
                st.session_state.confirm_caption_anyway = True
            else:
                st.session_state.confirm_caption_anyway = False
                st.session_state.page = "caption"
                st.rerun()

    # Confirm proceed if invalids remain
    if st.session_state.confirm_caption_anyway and len(st.session_state.invalid) > 0:
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("Proceed anyway (valid images only)"):
                st.session_state.page = "caption"
                st.session_state.caption_index = 0
                st.rerun()
        with cc2:
            if st.button("Cancel"):
                st.session_state.confirm_caption_anyway = False
                st.rerun()

# ===================== Filter Helpers (Captioning) =====================
def build_filters(valid_images, caps_map_full, landmarks_full):
    # Caption count options based on current valid images
    # compute caption counts per image
    counts = [len(caps_map_full.get(img, [])) for img in valid_images]
    max_count = max(counts) if counts else 0

    # dropdown options 0..max_count
    count_options = list(range(0, max_count + 1))

    # Landmarks: "Any" + sorted unique non-empty landmarks from valid images
    landmark_set = set()
    for img in valid_images:
        lm = landmarks_full.get(img, "")
        if lm:
            landmark_set.add(lm)
    landmark_options = ["Any"] + sorted(landmark_set)

    return count_options, landmark_options

def apply_filters(valid_images, caps_map_full, landmarks_full, cap_min, cap_max, landmark_filter):
    filtered = []
    for img in valid_images:
        cnt = len(caps_map_full.get(img, []))
        if cnt < cap_min or cnt > cap_max:
            continue
        lm = landmarks_full.get(img, "")
        if landmark_filter != "Any" and lm != landmark_filter:
            continue
        filtered.append(img)
    return filtered

# ===================== Page 3: Captioning =====================
def page_captioning():
    st.set_page_config(page_title="Captioning", layout="wide")
    folder = st.session_state.folder
    valid_images = st.session_state.valid
    shortname = st.session_state.shortname

    if not folder:
        st.session_state.page = "select"
        st.rerun()

    # Back button (preserve validation state)
    top_left, top_right = st.columns([4, 3])
    with top_left:
        st.markdown(f"### Captioning — `{folder}` | shortname: `{shortname}`")
        if st.button("🔙 Back to validation"):
            st.session_state.page = "validate"
            st.rerun()

    # Load CSVs fresh (source of truth)
    df_caps = ensure_csv(CAPTIONS_FILE, ["image_name", "caption"])
    df_land = ensure_csv(LANDMARKS_FILE, ["image_name", "landmark"])
    caps_map_full = captions_map(df_caps)
    landmarks_full = landmarks_map(df_land)

    # Build filter controls (top-right, compact)
    with top_right:
        st.markdown("#### Filters")
        # options
        count_options, landmark_options = build_filters(valid_images, caps_map_full, landmarks_full)

        # ensure current min/max exist in options
        cap_min_default = st.session_state.cap_min if st.session_state.cap_min in count_options else (count_options[0] if count_options else 0)
        cap_max_default = st.session_state.cap_max if st.session_state.cap_max in count_options else (count_options[-1] if count_options else 0)

        c_min = st.selectbox("Min captions", options=count_options if count_options else [0], index=(count_options.index(cap_min_default) if count_options else 0), key="filter_min")
        c_max = st.selectbox("Max captions", options=count_options if count_options else [0], index=(count_options.index(cap_max_default) if count_options else 0), key="filter_max")
        lm_choice = st.selectbox("Landmark", options=landmark_options, index=(landmark_options.index(st.session_state.landmark_filter) if st.session_state.landmark_filter in landmark_options else 0), key="filter_landmark")

        if st.button("Apply Filter"):
            # validation checks
            if c_min > c_max:
                st.warning("Min cannot be greater than Max.")
            else:
                st.session_state.cap_min = c_min
                st.session_state.cap_max = c_max
                st.session_state.landmark_filter = lm_choice
                st.session_state.filter_applied = True
                st.success("Filter applied.")

    # Compute filtered list
    if not valid_images:
        st.error("No valid images to caption. Go back and validate/rename first.")
        return

    filtered_images = apply_filters(
        valid_images,
        caps_map_full,
        landmarks_full,
        st.session_state.cap_min,
        st.session_state.cap_max,
        st.session_state.landmark_filter
    )

    if not filtered_images:
        st.warning("No images match the current filter. Adjust filters to see images.")
        return

    # Keep current index within bounds of filtered list
    idx = max(0, min(st.session_state.caption_index, len(filtered_images) - 1))
    st.session_state.caption_index = idx

    # Status line
    st.write(f"**Image {idx + 1} of {len(filtered_images)} (filtered)**")

    img_name = filtered_images[idx]
    img_path = os.path.join(folder, img_name)

    # Image + name + current landmark
    img = Image.open(img_path)
    st.image(img, caption=img_name, width=400)  # ~400px wide

    # Landmark display / edit
    current_landmark = landmarks_full.get(img_name, "")
    lm_col1, lm_col2, lm_col3 = st.columns([2, 2, 2])
    with lm_col1:
        st.write(f"**Landmark:** {current_landmark if current_landmark else '(none)'}")
    with lm_col2:
        new_landmark = st.text_input("Set / Change landmark:", value=current_landmark, key=f"lm_{img_name}")
    with lm_col3:
        if st.button("💾 Save Landmark"):
            # upsert in df_land
            df_land = ensure_csv(LANDMARKS_FILE, ["image_name", "landmark"])
            if img_name in df_land["image_name"].values:
                df_land.loc[df_land["image_name"] == img_name, "landmark"] = new_landmark.strip()
            else:
                df_land.loc[len(df_land)] = {"image_name": img_name, "landmark": new_landmark.strip()}
            save_csv_atomic(df_land, LANDMARKS_FILE)
            st.success("Landmark saved.")
            st.rerun()

    # Navigation
    nav_prev, nav_next = st.columns(2)
    with nav_prev:
        if st.button("⬅️ Prev"):
            if idx > 0:
                st.session_state.caption_index -= 1
                st.rerun()
    with nav_next:
        if st.button("➡️ Next"):
            if idx < len(filtered_images) - 1:
                st.session_state.caption_index += 1
                st.rerun()

    st.markdown("---")

    # Existing captions
    # Existing captions
    existing = caps_map_full.get(img_name, [])
    st.write(f"**Existing captions for this image:** {len(existing)}")
    if existing:
        with st.expander("Show previous captions"):
            for i, c in enumerate(existing, 1):
                col_cap, col_del = st.columns([8, 1])
                with col_cap:
                    st.write(f"{i}. {c}")
                with col_del:
                    if st.button(f"🗑️", key=f"del_{img_name}_{i}"):
                        # Delete this caption
                        df_caps = ensure_csv(CAPTIONS_FILE, ["image_name", "caption"])
                        # Remove only the specific caption (match by both image and caption text)
                        df_caps = df_caps[~((df_caps["image_name"] == img_name) & (df_caps["caption"] == c))]
                        save_csv_atomic(df_caps, CAPTIONS_FILE)
                        st.success("Caption deleted.")
                        st.rerun()

    # Add caption
    new_cap = st.text_input("Add a new caption:", key=f"cap_{img_name}")
    add_col, save_caps_col = st.columns([1, 1])
    with add_col:
        if st.button("➕ Add caption"):
            if new_cap.strip():
                df_caps = ensure_csv(CAPTIONS_FILE, ["image_name", "caption"])
                df_caps.loc[len(df_caps)] = {"image_name": img_name, "caption": new_cap.strip()}
                save_csv_atomic(df_caps, CAPTIONS_FILE)
                st.success("Caption added.")
                st.rerun()
            else:
                st.warning("Caption is empty.")
    with save_caps_col:
        if st.button("💾 Save captions file"):
            df_caps = ensure_csv(CAPTIONS_FILE, ["image_name", "caption"])
            save_csv_atomic(df_caps, CAPTIONS_FILE)
            st.success("Saved to captions.csv")

    st.markdown("---")

    # Summary sections based on filtered set
    df_caps = ensure_csv(CAPTIONS_FILE, ["image_name", "caption"])
    caps_map_full = captions_map(df_caps)
    imgs_with_caps = [i for i in filtered_images if len(caps_map_full.get(i, [])) > 0]
    imgs_without_caps = [i for i in filtered_images if len(caps_map_full.get(i, [])) == 0]

    s1, s2, s3 = st.columns(3)
    s1.info(f"Images with captions (filtered): **{len(imgs_with_caps)}**")
    s2.warning(f"Images without captions (filtered): **{len(imgs_without_caps)}**")
    s3.write(f"Total (filtered): **{len(filtered_images)}**")

    with st.expander("Images WITH captions (filtered)"):
        for i in imgs_with_caps:
            st.write(f"• {i} — {len(caps_map_full.get(i, []))} captions")
    with st.expander("Images WITHOUT captions (filtered)"):
        for i in imgs_without_caps:
            st.write(f"• {i}")

# ===================== App Controller =====================
def main():
    init_state()
    page = st.session_state.page
    if page == "select":
        page_select_folder()
    elif page == "validate":
        page_validation()
    elif page == "caption":
        page_captioning()
    else:
        st.session_state.page = "select"
        page_select_folder()

if __name__ == "__main__":
    main()
