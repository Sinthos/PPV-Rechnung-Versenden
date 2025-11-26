#!/bin/bash
#
# PPV Rechnung Versenden - Installation Script
# For Debian/Ubuntu LXC containers on Proxmox
#
# Usage: sudo bash install.sh
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
    echo "  PPV Rechnung Versenden - Installer"
    echo "  Automated Invoice Email System"
    echo "=============================================="
    echo ""
}

# Install system dependencies
install_dependencies() {
    log_info "Updating package lists..."
    apt-get update -qq

    log_info "Installing system dependencies..."
    apt-get install -y -qq \
        python3 \
        python3-venv \
        python3-pip \
        python3-dev \
        git \
        curl \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        zlib1g-dev \
        > /dev/null

    log_success "System dependencies installed"
}

# Create installation directory
create_directories() {
    log_info "Creating installation directory: ${INSTALL_DIR}"
    
    mkdir -p "${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}/data"
    
    log_success "Directories created"
}

# Clone or copy repository
setup_repository() {
    log_info "Setting up application files..."
    
    # Check if we're running from the repo directory
    if [ -f "requirements.txt" ] && [ -d "app" ]; then
        log_info "Copying files from current directory..."
        
        # Copy all files except .git and virtual environments
        rsync -av --exclude='.git' --exclude='venv' --exclude='__pycache__' \
            --exclude='*.pyc' --exclude='.env' --exclude='data' \
            ./ "${INSTALL_DIR}/"
        
        log_success "Files copied from local directory"
    elif [ -d "${INSTALL_DIR}/.git" ]; then
        log_info "Repository already exists, pulling latest changes..."
        cd "${INSTALL_DIR}"
        git pull
        log_success "Repository updated"
    else
        log_info "Cloning repository from ${REPO_URL}..."
        git clone "${REPO_URL}" "${INSTALL_DIR}"
        log_success "Repository cloned"
    fi
}

# Create Python virtual environment
setup_virtualenv() {
    log_info "Creating Python virtual environment..."
    
    cd "${INSTALL_DIR}"
    
    # Remove old venv if exists
    if [ -d "venv" ]; then
        log_warning "Removing existing virtual environment..."
        rm -rf venv
    fi
    
    python3 -m venv venv
    
    log_info "Installing Python dependencies..."
    source venv/bin/activate
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    deactivate
    
    log_success "Virtual environment created and dependencies installed"
}

# Setup environment file
setup_environment() {
    log_info "Setting up environment configuration..."
    
    cd "${INSTALL_DIR}"
    
    if [ ! -f ".env" ]; then
        if [ -f ".env.example" ]; then
            cp .env.example .env
            log_warning "Created .env from .env.example"
            log_warning "Please edit ${INSTALL_DIR}/.env with your Microsoft Graph API credentials!"
        else
            log_error ".env.example not found!"
            exit 1
        fi
    else
        log_info ".env file already exists, keeping existing configuration"
    fi
}

# Install systemd service
install_service() {
    log_info "Installing systemd service..."
    
    # Copy service file
    cp "${INSTALL_DIR}/ppv-rechnung.service" "/etc/systemd/system/${SERVICE_NAME}.service"
    
    # Reload systemd
    systemctl daemon-reload
    
    # Enable service
    systemctl enable "${SERVICE_NAME}.service"
    
    log_success "Systemd service installed and enabled"
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

# Print final instructions
print_instructions() {
    # Get the server IP
    SERVER_IP=$(hostname -I | awk '{print $1}')
    
    echo ""
    echo "=============================================="
    echo -e "${GREEN}  Installation Complete!${NC}"
    echo "=============================================="
    echo ""
    echo "  Web Interface: http://${SERVER_IP}:8000"
    echo ""
    echo "  Important Files:"
    echo "    - Config:    ${INSTALL_DIR}/.env"
    echo "    - Database:  ${INSTALL_DIR}/data/ppv_rechnung.db"
    echo "    - Logs:      journalctl -u ${SERVICE_NAME} -f"
    echo ""
    echo "  Service Commands:"
    echo "    - Status:    systemctl status ${SERVICE_NAME}"
    echo "    - Start:     systemctl start ${SERVICE_NAME}"
    echo "    - Stop:      systemctl stop ${SERVICE_NAME}"
    echo "    - Restart:   systemctl restart ${SERVICE_NAME}"
    echo "    - Logs:      journalctl -u ${SERVICE_NAME} -f"
    echo ""
    echo -e "${YELLOW}  IMPORTANT: Edit the .env file with your credentials:${NC}"
    echo "    nano ${INSTALL_DIR}/.env"
    echo ""
    echo "  Required settings in .env:"
    echo "    - TENANT_ID      (Azure AD Tenant ID)"
    echo "    - CLIENT_ID      (Azure AD App Client ID)"
    echo "    - CLIENT_SECRET  (Azure AD App Client Secret)"
    echo "    - SENDER_ADDRESS (Email address to send from)"
    echo ""
    echo "  After editing .env, restart the service:"
    echo "    systemctl restart ${SERVICE_NAME}"
    echo ""
    echo "=============================================="
}

# Main installation flow
main() {
    print_banner
    check_root
    
    log_info "Starting installation..."
    echo ""
    
    install_dependencies
    create_directories
    setup_repository
    setup_virtualenv
    setup_environment
    install_service
    start_service
    
    print_instructions
}

# Run main function
main "$@"
