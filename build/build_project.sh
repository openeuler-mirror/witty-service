#!/usr/bin/env bash

# Script to build the OpenHands project
# This script replicates the functionality of the 'make build' command

set -e  # Exit immediately if a command exits with a non-zero status

# ANSI color codes
GREEN=$(tput -Txterm setaf 2 2>/dev/null || echo "")
YELLOW=$(tput -Txterm setaf 3 2>/dev/null || echo "")
RED=$(tput -Txterm setaf 1 2>/dev/null || echo "")
BLUE=$(tput -Txterm setaf 6 2>/dev/null || echo "")
RESET=$(tput -Txterm sgr0 2>/dev/null || echo "")

# Default variables
PYTHON_VERSION=3.12
PRE_COMMIT_CONFIG_PATH="./dev_config/python/.pre-commit-config.yaml"

echo "${GREEN}Building project...${RESET}"

# Function to check system
check_system() {
    echo "${YELLOW}Checking system...${RESET}"
    if [ "$(uname)" = "Darwin" ]; then
        echo "${BLUE}macOS detected.${RESET}"
    elif [ "$(uname)" = "Linux" ]; then
        if [ -f "/etc/manjaro-release" ]; then
            echo "${BLUE}Manjaro Linux detected.${RESET}"
        else
            echo "${BLUE}Linux detected.${RESET}"
        fi
    elif [ "$(uname -r | grep -i microsoft)" ]; then
        echo "${BLUE}Windows Subsystem for Linux detected.${RESET}"
    else
        echo "${RED}Unsupported system detected. Please use macOS, Linux, or Windows Subsystem for Linux (WSL).${RESET}"
        exit 1
    fi
}

# Function to check Python installation
check_python() {
    echo "${YELLOW}Checking Python installation...${RESET}"
    if command -v python${PYTHON_VERSION} > /dev/null; then
        echo "${BLUE}$(python${PYTHON_VERSION} --version) is already installed.${RESET}"
    else
        echo "${RED}Python ${PYTHON_VERSION} is not installed. Please install Python ${PYTHON_VERSION} to continue.${RESET}"
        exit 1
    fi
}

# Function to check npm installation
check_npm() {
    echo "${YELLOW}Checking npm installation...${RESET}"
    if command -v npm > /dev/null; then
        echo "${BLUE}npm $(npm --version) is already installed.${RESET}"
    else
        echo "${RED}npm is not installed. Please install Node.js to continue.${RESET}"
        exit 1
    fi
}

# Function to check Node.js installation
check_nodejs() {
    echo "${YELLOW}Checking Node.js installation...${RESET}"
    if command -v node > /dev/null; then
        NODE_VERSION=$(node --version | sed -E 's/v//g')
        IFS='.' read -r -a NODE_VERSION_ARRAY <<< "$NODE_VERSION"
        if [ "${NODE_VERSION_ARRAY[0]}" -ge 22 ]; then
            echo "${BLUE}Node.js $NODE_VERSION is already installed.${RESET}"
        else
            echo "${RED}Node.js 22.x or later is required. Please install Node.js 22.x or later to continue.${RESET}"
            exit 1
        fi
    else
        echo "${RED}Node.js is not installed. Please install Node.js to continue.${RESET}"
        exit 1
    fi
}

# Function to check tmux installation
check_tmux() {
    echo "${YELLOW}Checking tmux installation...${RESET}"
    if command -v tmux > /dev/null; then
        echo "${BLUE}$(tmux -V) is already installed.${RESET}"
    else
        echo "${YELLOW}╔════════════════════════════════════════════════════════════════════════════╗${RESET}"
        echo "${YELLOW}║ OPTIONAL: tmux is not installed.                                          ║${RESET}"
        echo "${YELLOW}║ Some advanced terminal features may not work without tmux.                ║${RESET}"
        echo "${YELLOW}║ You can install it if needed, but it's not required for development.      ║${RESET}"
        echo "${YELLOW}╚════════════════════════════════════════════════════════════════════════════╝${RESET}"
    fi
}

# Function to check Poetry installation
check_poetry() {
    echo "${YELLOW}Checking Poetry installation...${RESET}"
    if command -v poetry > /dev/null; then
        POETRY_VERSION=$(poetry --version 2>&1 | sed -E 's/Poetry \(version ([0-9]+\.[0-9]+\.[0-9]+)\)/\1/')
        IFS='.' read -r -a POETRY_VERSION_ARRAY <<< "$POETRY_VERSION"
        if [ ${POETRY_VERSION_ARRAY[0]} -gt 1 ] || ([ ${POETRY_VERSION_ARRAY[0]} -eq 1 ] && [ ${POETRY_VERSION_ARRAY[1]} -ge 8 ]); then
            echo "${BLUE}$(poetry --version) is already installed.${RESET}"
        else
            echo "${RED}Poetry 1.8 or later is required. You can install poetry by running the following command, then adding Poetry to your PATH:${RESET}"
            echo "${RED} curl -sSL https://install.python-poetry.org | python${PYTHON_VERSION} -${RESET}"
            echo "${RED}More detail here: https://python-poetry.org/docs/#installing-with-the-official-installer${RESET}"
            exit 1
        fi
    else
        echo "${RED}Poetry is not installed. You can install poetry by running the following command, then adding Poetry to your PATH:${RESET}"
        echo "${RED} curl -sSL https://install.python-poetry.org | python${PYTHON_VERSION} -${RESET}"
        echo "${RED}More detail here: https://python-poetry.org/docs/#installing-with-the-official-installer${RESET}"
        exit 1
    fi
}

# Function to check dependencies
check_dependencies() {
    echo "${YELLOW}Checking dependencies...${RESET}"
    check_system
    check_python
    check_npm
    check_nodejs
    check_poetry
    check_tmux
    echo "${GREEN}Dependencies checked successfully.${RESET}"
}

# Function to install Python dependencies
install_python_dependencies() {
    echo "${GREEN}Installing Python dependencies...${RESET}"
    if [ -z "${TZ}" ]; then
        echo "Defaulting TZ (timezone) to UTC"
        export TZ="UTC"
    fi
    export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring

    # Check if we're building for RPM
    # RPM_BUILD_MODE can be set by build-rpm.sh or spec file
    if [ -n "${RPM_BUILD_MODE}" ] || [ -n "${RPM_BUILD}" ]; then
        echo "${BLUE}RPM build mode detected. Creating portable virtual environment...${RESET}"

        # 对于 RPM 构建，我们需要创建可移植的虚拟环境
        # 首先删除现有的 .venv 目录（如果存在）
        rm -rf .venv

        # 使用系统 Python 创建虚拟环境（使用 --copies 确保文件被复制而不是符号链接）
        if command -v python3.12 >/dev/null 2>&1; then
            python3.12 -m venv .venv
        elif command -v python3 >/dev/null 2>&1; then
            python3 -m venv .venv
        else
            echo "${RED}错误：未找到 Python 3.12 或 Python 3${RESET}"
            exit 1
        fi

        # 激活虚拟环境
        source .venv/bin/activate

        # 安装 poetry（如果不在虚拟环境中）
        if ! command -v poetry >/dev/null 2>&1; then
            pip3 install poetry==2.2.1
        fi

        # 配置 poetry 使用当前虚拟环境
        poetry config virtualenvs.create false

        # 安装运行时依赖
        echo "${BLUE}Installing runtime dependencies only...${RESET}"
        poetry install --only main --no-root

        # 停用虚拟环境
        deactivate
    else
        # 非 RPM 构建：使用 poetry 管理虚拟环境
        # Configure poetry to use in-project virtual environment (.venv/)
        poetry config virtualenvs.in-project true
        # Configure poetry to use --copies (always-copy) for virtual environment
        poetry config virtualenvs.options.always-copy true

        # 创建虚拟环境与指定 Python 版本
        poetry env use python${PYTHON_VERSION}

        if [ "$(uname)" = "Darwin" ]; then
            echo "${BLUE}Installing chroma-hnswlib...${RESET}"
            export HNSWLIB_NO_NATIVE=1
            poetry run pip install chroma-hnswlib
        fi
        if [ -n "${POETRY_GROUP}" ]; then
            echo "Installing only POETRY_GROUP=${POETRY_GROUP}"
            poetry install --only ${POETRY_GROUP}
        else
            poetry install --with dev,test,runtime
        fi
    fi
    if [ "${INSTALL_PLAYWRIGHT}" != "false" ] && [ "${INSTALL_PLAYWRIGHT}" != "0" ]; then
        if [ -f "/etc/manjaro-release" ]; then
            echo "${BLUE}Detected Manjaro Linux. Installing Playwright dependencies...${RESET}"
            poetry run pip install playwright
            poetry run playwright install chromium
        else
            if [ ! -f cache/playwright_chromium_is_installed.txt ]; then
                echo "Running playwright install --with-deps chromium..."
                poetry run playwright install --with-deps chromium
                mkdir -p cache
                touch cache/playwright_chromium_is_installed.txt
            else
                echo "Setup already done. Skipping playwright installation."
            fi
        fi
    else
        echo "Skipping Playwright installation (INSTALL_PLAYWRIGHT=${INSTALL_PLAYWRIGHT})."
    fi
    echo "${GREEN}Python dependencies installed successfully.${RESET}"
}

# Execute the build process
check_dependencies
install_python_dependencies

echo "${GREEN}Build completed successfully.${RESET}"
