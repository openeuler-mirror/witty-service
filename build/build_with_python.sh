#!/usr/bin/env bash

# Python源码地址：https://repo.huaweicloud.com/python/3.12.12/Python-3.12.12.tar.xz

set -e  # 任何命令失败则退出

# ANSI颜色代码
GREEN=$(tput -Txterm setaf 2 2>/dev/null || echo "")
YELLOW=$(tput -Txterm setaf 3 2>/dev/null || echo "")
RED=$(tput -Txterm setaf 1 2>/dev/null || echo "")
BLUE=$(tput -Txterm setaf 6 2>/dev/null || echo "")
RESET=$(tput -Txterm sgr0 2>/dev/null || echo "")

# 配置变量
PYTHON_VERSION="3.12.12"
PYTHON_SOURCE_URL="https://repo.huaweicloud.com/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz"
PYTHON_SOURCE_TAR="Python-${PYTHON_VERSION}.tar.xz"
PYTHON_SOURCE_DIR="Python-${PYTHON_VERSION}"
INSTALL_PREFIX="/usr/local/python-${PYTHON_VERSION}"  # 安装到系统目录
PROJECT_PYTHON_DIR="${PWD}/.venv"  # 项目内虚拟环境目录

# 日志函数
log_info() {
    echo "${GREEN}[INFO]${RESET} $1"
}

log_warn() {
    echo "${YELLOW}[WARN]${RESET} $1"
}

log_error() {
    echo "${RED}[ERROR]${RESET} $1"
}

log_step() {
    echo "${BLUE}[STEP]${RESET} $1"
}

# 检查命令是否存在
check_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        log_error "命令 '$1' 未安装。请安装后重试。"
        exit 1
    fi
}

# 检查依赖
check_dependencies() {
    log_step "检查系统依赖..."
    check_command tar
    check_command gcc
    check_command make
    check_command sudo
    log_info "系统依赖检查通过。"
}

download_file() {
    local url=$1
    local output=$2

    log_info "从 $url 下载..."

    if command -v curl >/dev/null 2>&1; then
        curl -L -o "$output" "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$output" "$url"
    else
        log_error "未找到curl或wget。请安装其中一个。"
        exit 1
    fi

    if [ $? -eq 0 ]; then
        log_info "下载完成: $output"
    else
        log_error "下载失败"
        exit 1
    fi
}

# 下载Python源码
download_python_source() {
    log_step "下载Python ${PYTHON_VERSION} 源码..."
    if [ -f "${PYTHON_SOURCE_TAR}" ]; then
        log_info "源码包已存在，跳过下载。"
    else
        download_file "${PYTHON_SOURCE_URL}" "${PYTHON_SOURCE_TAR}"
    fi
}

# 解压源码
extract_python_source() {
    log_step "解压Python源码..."
    if [ -d "${PYTHON_SOURCE_DIR}" ]; then
        log_info "源码目录已存在，跳过解压。"
    else
        tar -xf "${PYTHON_SOURCE_TAR}"
        log_info "解压完成。"
    fi
}

compile_and_install_python() {
    log_step "编译安装Python到 ${INSTALL_PREFIX}（优化体积）..."
    cd "${PYTHON_SOURCE_DIR}"

    # 配置 - 添加体积优化选项
    log_info "配置编译选项（优化体积）..."
    ./configure \
        --prefix="${INSTALL_PREFIX}" \
        --enable-optimizations \
        --with-lto \
        --disable-shared \
        --without-ensurepip \
        --disable-test-modules \
        --without-doc-strings \
        LDFLAGS="-Wl,-rpath=${INSTALL_PREFIX}/lib -Wl,--strip-all"

    export TZ=UTC
    # 编译（使用多核加速）
    log_info "开始编译（这可能需要几分钟）..."
    make -j$(nproc)

    log_info "安装到 ${INSTALL_PREFIX}..."
    make altinstall

    # 清理编译中间文件以减少体积
    log_info "清理编译中间文件..."
    make clean

    cd ..
    log_info "Python ${PYTHON_VERSION} 安装完成（体积优化版）。"
}

# 验证安装
verify_python_installation() {
    log_step "验证Python安装..."
    PYTHON_BIN="${INSTALL_PREFIX}/bin/python3.12"
    if [ -f "${PYTHON_BIN}" ]; then
        log_info "Python 3.12 已安装: ${PYTHON_BIN}"
        "${PYTHON_BIN}" --version
        # 添加到PATH以便后续使用
        export PATH="${INSTALL_PREFIX}/bin:${PATH}"
        log_info "已将 ${INSTALL_PREFIX}/bin 添加到PATH"
    else
        log_error "Python 3.12 未找到，安装可能失败。"
        exit 1
    fi
}

# 构建当前项目
build_current_project() {
    log_step "构建当前项目..."

    # 确保使用新安装的Python
    PYTHON_BIN="${INSTALL_PREFIX}/bin/python3.12"
    if [ ! -f "${PYTHON_BIN}" ]; then
        log_error "未找到新安装的Python二进制文件: ${PYTHON_BIN}"
        exit 1
    fi

    # 设置环境变量，使用新安装的Python
    export PATH="${INSTALL_PREFIX}/bin:${PATH}"
    export PYTHON="${PYTHON_BIN}"
    export PYTHON_VERSION=3.12

    log_info "使用Python: ${PYTHON_BIN}"
    "${PYTHON_BIN}" --version

    # 检查是否存在构建脚本
    if [ -f "build/build_project.sh" ]; then
        log_info "使用现有的 build/build_project.sh 脚本..."
        ./build/build_project.sh
    else
        log_warn "未找到 build/build_project.sh，尝试使用 make build..."
        if [ -f "Makefile" ]; then
            make build
        else
            log_error "未找到构建脚本或Makefile，跳过项目构建。"
        fi
    fi
}

# 将Python构建到当前项目内
build_python_into_project() {
    log_step "将Python构建到当前项目内..."

    # 创建项目内Python目录
    mkdir -p "${PROJECT_PYTHON_DIR}"

    # 复制Python二进制文件
    log_info "复制Python二进制文件..."
    PYTHON_BIN="${INSTALL_PREFIX}/bin/python3.12"
    if [ -f "${PYTHON_BIN}" ]; then
        cp "${PYTHON_BIN}" "${PROJECT_PYTHON_DIR}/"

        # 复制相关库文件（简化版：只复制二进制文件）
        log_info "创建符号链接..."
        cd "${PROJECT_PYTHON_DIR}"
        ln -sf python3.12 python
        ln -sf python3.12 python3
        cd ..

        log_info "Python已复制到 ${PROJECT_PYTHON_DIR}"
    else
        log_error "无法找到Python二进制文件，跳过复制。"
        return 1
    fi

    log_info "项目内Python构建完成。"
}

# 清理临时文件（可选）
cleanup() {
    log_step "清理临时文件..."
    if [ -d "${PYTHON_SOURCE_DIR}" ]; then
        log_info "删除源码目录 ${PYTHON_SOURCE_DIR}..."
        rm -rf "${PYTHON_SOURCE_DIR}"
    fi
    if [ -f "${PYTHON_SOURCE_TAR}" ]; then
        log_info "保留源码包 ${PYTHON_SOURCE_TAR} 以便后续使用。"
    fi
}

# 主函数
main() {
    log_step "开始执行Python构建脚本..."

    # 检查依赖
    check_dependencies

    # 下载和解压
    download_python_source
    extract_python_source

    # 编译安装
    compile_and_install_python

    # 验证
    verify_python_installation

    # 构建当前项目
    build_current_project

    # 将Python构建到项目内
    build_python_into_project

    # 清理
    # cleanup

    log_step "所有步骤完成！"
    log_info "Python ${PYTHON_VERSION} 已安装到 ${INSTALL_PREFIX}"
    log_info "项目内Python位于 ${PROJECT_PYTHON_DIR}"
    log_info "可以使用 ${PROJECT_PYTHON_DIR}/python 运行Python。"
}

# 执行主函数
main
