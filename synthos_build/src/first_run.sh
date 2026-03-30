#!/bin/bash
# first_run.sh — Synthos First Run Setup
# Run this once after cloning the repo:
#   bash /home/pi/synthos/first_run.sh
#
# After running this you can type:
#   install    — from anywhere to launch the installer
#   synthos     — same thing (registered by installer on completion)

SYNTHOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "============================================================"
echo "  SYNTHOS — FIRST RUN SETUP"
echo "============================================================"

# Register 'install' command system-wide
sudo bash -c "echo '#!/bin/bash
cd $SYNTHOS_DIR/src && python3 install_retail.py \"\$@\"' > /usr/local/bin/install && chmod +x /usr/local/bin/install"

if [ $? -eq 0 ]; then
    echo ""
    echo "  ✓ 'install' command registered"
    echo ""
    echo "  Type 'install' from anywhere to launch Synthos setup."
    echo "  Browser will open automatically."
    echo ""
    echo "============================================================"
    echo ""
else
    echo ""
    echo "  Could not register system command."
    echo "  Run the installer manually:"
    echo "    python3 $SYNTHOS_DIR/src/install_retail.py"
    echo ""
fi
