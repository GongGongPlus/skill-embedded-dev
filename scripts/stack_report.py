#!/usr/bin/env python3
"""stack_report.py — 解析 FreeRTOS uxTaskGetSystemState / vTaskList 输出，
报告各任务栈水位，剩余 < 阈值即告警。

用法 1（粘贴 vTaskList 输出）:  python stack_report.py --tasklist tasklist.txt --min-free 512
用法 2（粘贴 Task State 表）:   从串口直接粘贴到 stdin:  python stack_report.py --min-free 512

vTaskList 输出格式（空格分隔）:  任务名  状态  优先级  剩余栈(字)  序号
单位说明: 剩余栈单位是 StackType_t (通常 uint32_t=字, 1 字=4 字节)。
"""
import argparse
import sys

WARNING_LINE = "!! 低于阈值，需要排查"

def parse(lines):
    tasks = []
    for ln in lines:
        ln = ln.strip()
        if not ln or set(ln) <= set("-= "):
            continue
        parts = ln.split()
        if len(parts) < 4:
            continue
        name = parts[0]
        # 取最后两个数字中的倒数第二个为剩余栈（兼容行尾序号缺失）
        nums = [p for p in parts[1:] if p.isdigit()]
        if len(nums) < 2:
            continue
        free_words = int(nums[-2])
        tasks.append((name, free_words))
    return tasks

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasklist", help="vTaskList 输出文件；缺省读 stdin")
    ap.add_argument("--min-free", type=int, default=512,
                    help="告警阈值（字），默认 512 字 = 2048 字节")
    ap.add_argument("--word-bytes", type=int, default=4)
    a = ap.parse_args()

    if a.tasklist:
        with open(a.tasklist, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()

    tasks = parse(lines)
    if not tasks:
        print("未解析到任务行——确认输入是 vTaskList/uxTaskGetSystemState 的表格输出")
        return 1

    print(f"{'任务':<20}{'剩余栈(字)':>12}{'剩余栈(字节)':>14}  状态")
    worst = None
    for name, free in tasks:
        status = "OK"
        if free < a.min_free:
            status = WARNING_LINE
        print(f"{name:<20}{free:>12}{free * a.word_bytes:>14}  {status}")
        if worst is None or free < worst[1]:
            worst = (name, free)

    if worst:
        print(f"\n最低水位: {worst[0]} 仅剩 {worst[1]*a.word_bytes} 字节"
              f"（阈值 {a.min_free*a.word_bytes} 字节）")
    return 0

if __name__ == "__main__":
    sys.exit(main())
