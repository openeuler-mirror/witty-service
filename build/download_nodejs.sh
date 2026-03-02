#!/usr/bin/env bash

# Script to download Node.js from Huawei Cloud Mirror
# Supports multiple architectures and operating systems

set -e  # Exit immediately if a command exits with a non-zero status

# ANSI color codes
GREEN=$(tput -Txterm setaf 2 2>/dev/null || echo "")
YELLOW=$(tput -Txterm setaf 3 2>/dev/null || echo "")
RED=$(tput -Txterm setaf 1 2>/dev/null || echo "")
BLUE=$(tput -Txterm setaf 6 2>/dev/null || echo "")
RESET=$(tput -Txterm sgr0 2>/dev/null || echo "")

# Configuration
NODE_VERSION="v22.22.0"
MIRROR_URL="https://mirrors.huaweicloud.com/nodejs/${NODE_VERSION}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-./nodejs}"
EXTRACT_DIR="${EXTRACT_DIR:-./nodejs}"

# Function to print colored messages
log_info() {
    echo "${GREEN}[INFO]${RESET} $1"
}

log_warn() {
    echo "${YELLOW}[WARN]${RESET} $1"
}

log_error() {
    echo "${RED}[ERROR]${RESET} $1"
}

# Function to detect system architecture and OS
detect_system() {
    local os
    local arch

    # Detect OS
    case "$(uname -s)" in
        Linux*)     os="linux" ;;
        Darwin*)    os="darwin" ;;
        CYGWIN*|MINGW*|MSYS*) os="win" ;;
        AIX*)       os="aix" ;;
        *)          os="unknown" ;;
    esac

    # Detect architecture
    case "$(uname -m)" in
        x86_64|amd64)   arch="x64" ;;
        i686|i386)      arch="x86" ;;
        aarch64|arm64)  arch="arm64" ;;
        armv7l|armv8l)  arch="armv7l" ;;
        ppc64le)        arch="ppc64le" ;;
        s390x)          arch="s390x" ;;
        ppc64)          arch="ppc64" ;;
        *)              arch="unknown" ;;
    esac

    echo "${os}-${arch}"
}

# Function to map system to Node.js package filename
get_package_filename() {
    local system=$1
    local prefer_xz=${2:-true}

    case $system in
        linux-x64)
            if [ "$prefer_xz" = "true" ] && command -v xz >/dev/null 2>&1; then
                echo "node-${NODE_VERSION}-linux-x64.tar.xz"
            else
                echo "node-${NODE_VERSION}-linux-x64.tar.gz"
            fi
            ;;
        linux-arm64)
            if [ "$prefer_xz" = "true" ] && command -v xz >/dev/null 2>&1; then
                echo "node-${NODE_VERSION}-linux-arm64.tar.xz"
            else
                echo "node-${NODE_VERSION}-linux-arm64.tar.gz"
            fi
            ;;
        linux-armv7l)
            if [ "$prefer_xz" = "true" ] && command -v xz >/dev/null 2>&1; then
                echo "node-${NODE_VERSION}-linux-armv7l.tar.xz"
            else
                echo "node-${NODE_VERSION}-linux-armv7l.tar.gz"
            fi
            ;;
        linux-ppc64le)
            if [ "$prefer_xz" = "true" ] && command -v xz >/dev/null 2>&1; then
                echo "node-${NODE_VERSION}-linux-ppc64le.tar.xz"
            else
                echo "node-${NODE_VERSION}-linux-ppc64le.tar.gz"
            fi
            ;;
        linux-s390x)
            if [ "$prefer_xz" = "true" ] && command -v xz >/dev/null 2>&1; then
                echo "node-${NODE_VERSION}-linux-s390x.tar.xz"
            else
                echo "node-${NODE_VERSION}-linux-s390x.tar.gz"
            fi
            ;;
        darwin-arm64)
            if [ "$prefer_xz" = "true" ] && command -v xz >/dev/null 2>&1; then
                echo "node-${NODE_VERSION}-darwin-arm64.tar.xz"
            else
                echo "node-${NODE_VERSION}-darwin-arm64.tar.gz"
            fi
            ;;
        darwin-x64)
            if [ "$prefer_xz" = "true" ] && command -v xz >/dev/null 2>&1; then
                echo "node-${NODE_VERSION}-darwin-x64.tar.xz"
            else
                echo "node-${NODE_VERSION}-darwin-x64.tar.gz"
            fi
            ;;
        win-x64)
            echo "node-${NODE_VERSION}-win-x64.zip"
            ;;
        win-x86)
            echo "node-${NODE_VERSION}-win-x86.zip"
            ;;
        win-arm64)
            echo "node-${NODE_VERSION}-win-arm64.zip"
            ;;
        aix-ppc64)
            echo "node-${NODE_VERSION}-aix-ppc64.tar.gz"
            ;;
        *)
            log_error "Unsupported system: $system"
            log_error "Available systems: linux-x64, linux-arm64, linux-armv7l, linux-ppc64le, linux-s390x, darwin-arm64, darwin-x64, win-x64, win-x86, win-arm64, aix-ppc64"
            exit 1
            ;;
    esac
}

# Function to download file
download_file() {
    local url=$1
    local output=$2

    log_info "Downloading from $url"

    if command -v curl >/dev/null 2>&1; then
        curl -L -o "$output" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$output" "$url"
    else
        log_error "Neither curl nor wget found. Please install one of them."
        exit 1
    fi

    if [ $? -eq 0 ]; then
        log_info "Download completed: $output"
    else
        log_error "Download failed"
        exit 1
    fi
}

# Function to extract file
extract_file() {
    local file=$1
    local target_dir=$2

    log_info "Extracting $file to $target_dir"

    mkdir -p "$target_dir"

    case "$file" in
        *.tar.gz|*.tgz)
            tar -xzf "$file" -C "$target_dir" --strip-components=1
            ;;
        *.tar.xz)
            tar -xJf "$file" -C "$target_dir" --strip-components=1
            ;;
        *.zip)
            if command -v unzip >/dev/null 2>&1; then
                unzip -q "$file" -d "$target_dir"
                # Move contents from subdirectory
                if [ -d "$target_dir/node-${NODE_VERSION}" ]; then
                    mv "$target_dir/node-${NODE_VERSION}"/* "$target_dir/"
                    rmdir "$target_dir/node-${NODE_VERSION}"
                fi
            else
                log_error "unzip command not found. Please install unzip."
                exit 1
            fi
            ;;
        *)
            log_error "Unsupported file format: $file"
            exit 1
            ;;
    esac

    log_info "Extraction completed"
}

# Function to verify checksum (optional)
verify_checksum() {
    local file=$1

    log_info "Verifying checksum for $file"

    # Download SHA256 checksum file
    local checksum_url="${MIRROR_URL}/SHASUMS256.txt"
    local checksum_file="/tmp/SHASUMS256.txt"

    if command -v curl >/dev/null 2>&1; then
        curl -L -s "$checksum_url" -o "$checksum_file"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$checksum_file" "$checksum_url"
    else
        log_warn "Cannot download checksum file (curl/wget not found)"
        return 0
    fi

    if [ ! -f "$checksum_file" ]; then
        log_warn "Checksum file not downloaded, skipping verification"
        return 0
    fi

    # Get the checksum for our file
    local filename=$(basename "$file")
    local expected_checksum=$(grep "$filename" "$checksum_file" | awk '{print $1}')

    if [ -z "$expected_checksum" ]; then
        log_warn "Checksum not found for $filename, skipping verification"
        return 0
    fi

    # Calculate actual checksum
    if command -v sha256sum >/dev/null 2>&1; then
        local actual_checksum=$(sha256sum "$file" | awk '{print $1}')
    elif command -v shasum >/dev/null 2>&1; then
        local actual_checksum=$(shasum -a 256 "$file" | awk '{print $1}')
    else
        log_warn "sha256sum or shasum not found, skipping verification"
        return 0
    fi

    if [ "$expected_checksum" = "$actual_checksum" ]; then
        log_info "Checksum verification passed"
    else
        log_error "Checksum verification failed"
        log_error "Expected: $expected_checksum"
        log_error "Actual:   $actual_checksum"
        exit 1
    fi
}

# Main function
main() {
    log_info "Node.js Download Script"
    log_info "Version: ${NODE_VERSION}"
    log_info "Mirror: ${MIRROR_URL}"

    # Detect system
    local system=$(detect_system)
    log_info "Detected system: $system"

    # Get package filename
    local package_name=$(get_package_filename "$system")
    log_info "Package: $package_name"

    # Create download directory
    mkdir -p "$DOWNLOAD_DIR"

    # Download URL
    local download_url="${MIRROR_URL}/${package_name}"
    local output_file="${DOWNLOAD_DIR}/${package_name}"

    # Download the package
    download_file "$download_url" "$output_file"

    # Verify checksum (optional)
    verify_checksum "$output_file"

    # Extract the package
    extract_file "$output_file" "$EXTRACT_DIR"

    # Display success message
    log_info "Node.js ${NODE_VERSION} has been successfully downloaded and extracted to ${EXTRACT_DIR}"
    log_info "You can add ${EXTRACT_DIR}/bin to your PATH"

    # Show node version
    if [ -f "${EXTRACT_DIR}/bin/node" ]; then
        "${EXTRACT_DIR}/bin/node" --version
    fi
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --download-dir)
            DOWNLOAD_DIR="$2"
            shift 2
            ;;
        --extract-dir)
            EXTRACT_DIR="$2"
            shift 2
            ;;
        --no-xz)
            PREFER_XZ=false
            shift
            ;;
        --version)
            echo "Node.js Download Script v1.0"
            exit 0
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo "Download Node.js from Huawei Cloud Mirror"
            echo ""
            echo "Options:"
            echo "  --download-dir DIR   Directory to save downloaded package (default: ./nodejs)"
            echo "  --extract-dir DIR    Directory to extract Node.js (default: ./nodejs)"
            echo "  --no-xz              Prefer .tar.gz over .tar.xz even if xz is available"
            echo "  --version            Show version information"
            echo "  --help               Show this help message"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Run main function
main
