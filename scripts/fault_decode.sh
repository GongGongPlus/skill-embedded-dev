#!/usr/bin/env bash
# fault_decode.sh — 把 HardFault/Guru Meditation 的 PC/LR 地址解码为源码位置
# 用法: ./fault_decode.sh <firmware.elf> <addr1> [addr2 ...]
# 工具链自动探测: ESP32 (xtensa) / ARM (arm-none-eabi) / 原生 addr2line
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "用法: $0 <firmware.elf> <addr1> [addr2 ...]"; exit 1
fi
ELF="$1"; shift

pick_tool() {
    if command -v xtensa-esp32s3-elf-addr2line >/dev/null 2>&1; then
        echo xtensa-esp32s3-elf-addr2line
    elif command -v xtensa-esp32-elf-addr2line >/dev/null 2>&1; then
        echo xtensa-esp32-elf-addr2line
    elif command -v arm-none-eabi-addr2line >/dev/null 2>&1; then
        echo arm-none-eabi-addr2line
    elif command -v addr2line >/dev/null 2>&1; then
        echo addr2line
    else
        echo "错误: 未找到任何 addr2line 工具链" >&2; exit 2
    fi
}

TOOL="$(pick_tool)"
echo "[info] 使用工具链: $TOOL"
echo "[info] 镜像: $ELF"
echo "[info] 确认该 ELF 与板上烧录镜像为同一次构建产物，否则结果不可信。"
echo "--------"
"$TOOL" -pf -C -e "$ELF" "$@"
