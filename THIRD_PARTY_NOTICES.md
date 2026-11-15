# Third-party notices / 第三方声明

This repository's original integration, routing instructions, documentation,
and helper code are released under the repository-level MIT License. Names of
third-party products, standards, libraries, platforms, contests, and projects
remain the property of their respective owners and are referenced for
interoperability, research, or educational purposes. A file carrying its own
copyright or license notice remains governed by that notice.

本仓库原创的整合、路由说明、文档与辅助代码采用仓库根目录的 MIT 许可证。文中提及的
第三方产品、标准、库、平台、赛事与项目仍归其各自权利人所有，仅用于互操作、研究或
教学说明。若某个文件带有独立版权或许可证声明，则该文件以其自身声明为准。

## Interface ideas studied / 借鉴的接口思路

No source code from the projects below is included in this repository. Their public
interfaces, report contracts, and workflow ideas were studied to design this skill's own
implementation; the MIT/Apache-2.0 implementations remain copyright their authors.

本仓库未复制下列项目的源码，仅研究其公开接口、报告契约与工作流思路后自行实现：

- **serial-protocol-tester-skill / serial-protocol-tester-app** (10walnut) — PolyForm
  Noncommercial 1.0.0: specification-driven serial protocol decoding and a desktop
  harness. Only the *idea* of a JSON protocol specification plus a validator was
  borrowed; no code was copied, and this repository's MIT license does not relicense it.
- **STM32_AutoDebug_Universal_Kit** (TaoCosmo-Dev, MIT) — exit-code contract, diagnostic
  report with `ai_repair_prompt`/`repeated_failure`/`source_unchanged`, and a
  build-flash-token HIL loop.
- **Lissio** (Horldsence, Apache-2.0) — CAN ISO-TP/UDS/OBD-II/J1939 decoding scope and
  the MCP-exposed tool surface.
- **llm-pid-tuner** (KINGSTON-115, MIT) — bounded PID update validation, the telemetry
  metric contract, and the compact trial-record idea behind `tune`. Only the interface
  and workflow design were reimplemented; no source code was copied.
- **yoma** (yoma-embedded; MIT per its `package.json`, while GitHub metadata reports
  NOASSERTION) — the controller-map/netlist pin-tracing scope, the evidence-graded project
  memory model, the evidence-scope behavior cases, and the "AI decides configuration, the
  kernel generates code" separation. Only ideas were reimplemented; no source was copied.
- **embeddedskills**, **embedded-workbench**, **dsh-stm32** and other public embedded
  skills — reviewed for comparison of tool coverage and workflow discipline; no files
  were copied.

Repository maintainer / 仓库维护者：**中北大学机器人协会续晋全**
