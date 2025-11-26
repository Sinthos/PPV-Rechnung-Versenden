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
    
    if [ ! -f "${INSTALL_DIR}/requirements.txt" ]; then
        log_error "Invalid installation - requirements.txt not found"
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

# Update from Git or local files
update_files() {
    log_info "Updating application files..."
    
    cd "${INSTALL_DIR}"
    
    # Check if this is a git repository
    if [ -d ".git" ]; then
        log_info "Pulling latest changes from Git..."
        
        # Stash any local changes
        git stash --quiet 2>/dev/null || true
        
        # Pull latest changes
        git pull
        
        log_success "Git repository updated"
    else
        # Check if we're running from a source directory
        SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        
        if [ -f "${SCRIPT_DIR}/requirements.txt" ] && [ -d "${SCRIPT_DIR}/app" ]; then
            log_info "Copying files from ${SCRIPT_DIR}..."
            
            # Copy all files except .git, venv, .env, and data
            rsync -av --exclude='.git' --exclude='venv' --exclude='__pycache__' \
                --exclude='*.pyc' --exclude='.env' --exclude='data' \
                "${SCRIPT_DIR}/" "${INSTALL_DIR}/"
            
            log_success "Files copied from local directory"
        else
            log_warning "No Git repository and no local source found"
            log_warning "Skipping file update, only updating dependencies"
        fi
    fi
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
    pip install --upgrade pip -q
    pip install -r requirements.txt -q --upgrade
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
