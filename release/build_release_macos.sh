#!/usr/bin/env bash
set -euo pipefail

project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
app="$project/app"
build="$project/release_build"

rm -rf "$build"
mkdir -p "$build"

python3 -m PyInstaller --clean --noconfirm --onedir --windowed \
  --name S2Dashboard \
  --distpath "$build/dist" \
  --workpath "$build/work" \
  --specpath "$build" \
  --paths "$app" \
  --add-data "$app/frontend:frontend" \
  --add-data "$app/config/fund_whitelist.json:config" \
  --add-data "$app/config/fund_fee_config.json:config" \
  --add-data "$app/contracts:contracts" \
  --add-data "$app/backend/formula_contract.json:backend" \
  --add-data "$app/data/s2_v2_monthly.csv:data" \
  --add-data "$app/data/s2_performance.csv:data" \
  --add-data "$app/data/s2_crisis_summary.csv:data" \
  --add-data "$app/data_source_contract.json:." \
  --add-data "$app/assets/s2-dashboard-icon.png:assets" \
  --hidden-import backend.data_updater \
  --hidden-import backend.data_providers.fund_nav_provider \
  --hidden-import backend.data_providers.csi_provider \
  --hidden-import backend.data_providers.chinabond_provider \
  --hidden-import akshare \
  "$app/backend/release_entry.py"

dist="$build/dist/S2Dashboard.app"
test -d "$dist"
cp "$app/release/README.txt" "$dist/Contents/Resources/README.txt"

mkdir -p "$build/artifacts"
ditto -c -k --sequesterRsrc --keepParent "$dist" "$build/artifacts/S2Dashboard_macOS.zip"
if command -v hdiutil >/dev/null 2>&1; then
  hdiutil create -volname "S2 Dashboard" -srcfolder "$dist" -ov -format UDZO "$build/artifacts/S2Dashboard_macOS.dmg"
fi
printf '%s\n' "$build/artifacts"
