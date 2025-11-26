#!/bin/bash
#
# PPV Rechnung Versenden - Update Script
# Updates the application from GitHub and restarts the service
#
# Usage: sudo bash update.sh
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
INSTALL_DIR="/opt/ppv-rechnung"
SERVICE_NAME="ppv-rechnung"
REPO_URL="https://github.com/Sinthos/PPV-Rechnung-Versenden.git"
BRANCH="main"

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# Print banner
print_banner() {
    echo ""
    echo "=============================================="
    echo "  PPV Rechnung Versenden - Updater"
    echo "=============================================="
    echo ""
}

# Check if installation exists
check_installation() {
    if [ ! -d "${INSTALL_DIR}" ]; then
        log_error "Installation not found at ${INSTALL_DIR}"
        log_error "Please run install.sh first"
        exit 1
    fi
}

# Stop the service
stop_service() {
    log_info "Stopping ${SERVICE_NAME} service..."
    
    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
        systemctl stop "${SERVICE_NAME}.service"
        log_success "Service stopped"
    else
        log_warning "Service was not running"
    fi
}

# Update from Git
update_files() {
    log_info "Updating application files..."
    
    # Check if INSTALL_DIR is a git repository
    if [ -d "${INSTALL_DIR}/.git" ]; then
        log_info "Git repository detected at ${INSTALL_DIR}"
        cd "${INSTALL_DIR}"
        
        # Fetch latest changes
        log_info "Fetching changes from GitHub..."
        git fetch --all
        
        # Reset to match remote (force update)
        log_info "Resetting local files to match remote branch '${BRANCH}'..."
        git reset --hard "origin/${BRANCH}"
        
        log_success "Files updated via Git"
    else
        log_warning "Installation directory is NOT a Git repository."
        log_info "Downloading latest version from GitHub to temporary location..."
        
        TEMP_DIR=$(mktemp -d)
        
        # Clone to temp dir
        if git clone -b "${BRANCH}" "${REPO_URL}" "${TEMP_DIR}"; then
            log_info "Cloned repository successfully."
            
            log_info "Replacing files in ${INSTALL_DIR}..."
            # Sync files, deleting extraneous ones in destination (excluding config/data)
            # We exclude:
            # - .env (configuration)
            # - data/ (database and local storage)
            # - venv/ (virtual environment)
            # - __pycache__/
            # - .git/ (don't make the install dir a git repo if it wasn't)
            
            rsync -av --delete \
                --exclude='.env' \
                --exclude='data/' \
                --exclude='venv/' \
                --exclude='.git/' \
                --exclude='__pycache__/' \
                --exclude='*.pyc' \
                "${TEMP_DIR}/" "${INSTALL_DIR}/"
            
            rm -rf "${TEMP_DIR}"
            log_success "Files updated from GitHub tarball/clone"
        else
            log_error "Failed to clone repository. Check internet connection."
            rm -rf "${TEMP_DIR}"
            exit 1
        fi
    fi
    
    # Fix permissions
    log_info "Fixing permissions..."
    chown -R root:root "${INSTALL_DIR}"
    # Ensure data dir is writable (if app runs as different user, adjust here)
    # Usually app runs as root or specific user. Assuming root for service based on install.sh check?
    # If user 'ppv' exists, chown to it?
    # For now, assuming standard install.
}

# Update Python dependencies
update_dependencies() {
    log_info "Updating Python dependencies..."
    
    cd "${INSTALL_DIR}"
    
    if [ ! -d "venv" ]; then
        log_warning "Virtual environment not found, creating new one..."
        python3 -m venv venv
    fi
    
    source venv/bin/activate
    pip install --upgrade pip
    # Install dependencies (including new smbprotocol)
    pip install -r requirements.txt
    deactivate
    
    log_success "Dependencies updated"
}

# Update systemd service file if changed
update_service() {
    log_info "Checking systemd service file..."
    
    if [ -f "${INSTALL_DIR}/ppv-rechnung.service" ]; then
        # Compare with installed service file
        if ! cmp -s "${INSTALL_DIR}/ppv-rechnung.service" "/etc/systemd/system/${SERVICE_NAME}.service"; then
            log_info "Updating systemd service file..."
            cp "${INSTALL_DIR}/ppv-rechnung.service" "/etc/systemd/system/${SERVICE_NAME}.service"
            systemctl daemon-reload
            log_success "Service file updated"
        else
            log_info "Service file unchanged"
        fi
    fi
}

# Start the service
start_service() {
    log_info "Starting ${SERVICE_NAME} service..."
    
    systemctl start "${SERVICE_NAME}.service"
    
    # Wait a moment for the service to start
    sleep 2
    
    # Check if service is running
    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
        log_success "Service started successfully"
    else
        log_error "Service failed to start. Check logs with: journalctl -u ${SERVICE_NAME} -f"
        # Ask for reboot if failed
        read -p "Service failed to start. Do you want to reboot the system? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            reboot
        fi
        exit 1
    fi
}

# Print completion message
print_completion() {
    SERVER_IP=$(hostname -I | awk '{print $1}')
    
    echo ""
    echo "=============================================="
    echo -e "${GREEN}  Update Complete!${NC}"
    echo "=============================================="
    echo ""
    echo "  Web Interface: http://${SERVER_IP}:8000"
    echo ""
    echo "  Check service status:"
    echo "    systemctl status ${SERVICE_NAME}"
    echo ""
    echo "  View logs:"
    echo "    journalctl -u ${SERVICE_NAME} -f"
    echo ""
    echo "=============================================="
}

# Main update flow
main() {
    print_banner
    check_root
    check_installation
    
    log_info "Starting update..."
    echo ""
    
    stop_service
    update_files
    update_dependencies
    update_service
    start_service
    
    print_completion
}

# Run main function
main "$@"
