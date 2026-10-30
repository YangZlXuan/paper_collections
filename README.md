# 论文批量下载助手

Python 桌面软件，导入 DOI 文件、RIS / BibTeX 引文和 ScienceDirect 文章链接，解析、去重并批量下载。当前版本为 1.8.0：支持启动后台检查更新及手动检查；Springer / Nature 支持正文 PDF 和补充材料；Elsevier / ScienceDirect 通过官方 API 下载正文 PDF 和补充材料（SI）；Wiley 正文通过官方 TDM API 下载，SI 通过 Crossref 元数据辅助发现并尝试获取。其余出版社可导入、预览和导出清单，下载入口暂未启用。

## 直接使用 EXE

运行 `PaperDownloader.exe` 即可使用，无需安装 Python 或 Conda。更新时请先完成当前任务并关闭旧版，再启动新版。EXE 可单独复制到其他 Windows 64 位电脑运行。

1. 选择 **Springer Nature**、**Elsevier** 或 **Wiley**，选择一本期刊或“全部期刊（该出版社）”。可在期刊框输入关键词后展开列表选择。
2. 选择文献文件，软件自动解析并去重。支持 TXT、CSV、TSV、XLSX、RIS、BIB / BIBTEX；也可直接粘贴 DOI 或 ScienceDirect 文章链接进行解析。导入的标题、期刊、DOI、PII 和链接可在列表和详情中查看，整理后的清单可导出为 CSV。
3. Elsevier 自动读取当前用户环境变量 **`ElsevierKey`**，Wiley 自动读取 **`WileyKey`**，无需在界面重复填写。Springer 可在访问设置中选择 Netscape 格式 Cookie、填写可选联系邮箱；机构提供 Elsevier Insttoken 时也可在这里填写。设置环境变量后需重新启动软件；通过终端或 IDE 启动时也需重启终端或 IDE。
4. 选择保存目录和“下载间隔（秒）”，按需勾选“下载补充材料”，点击“开始下载”。Springer、Elsevier、Wiley 的补充材料开关独立保存，默认均开启。Springer / Elsevier 间隔为 2–60 秒（默认 2），Wiley 为 10–60 秒（默认 10），独立保存。Wiley SI 依赖 Crossref 实际登记的线索，不保证所有论文都有可下载记录。
5. 在论文列表查看每篇的状态，选中记录查看完整错误原因，双击已下载的记录可打开 PDF；“打开文章页面”可用于核对网页上的 Supporting Information。
6. 完成后点击“打开结果清单”查看 CSV。每篇论文目录还有 `manifest.json`，记录正文与每个补充文件的结果。取消后重新导入同一文件、选择同一目录，可跳过通过校验的已有文件。

Springer 下载器通过出版社文章页面发现下载地址。`10.1038/` DOI 定位到 Nature 文章页，其余 DOI 定位到 Springer 文章页，从页面元数据和链接发现正文及补充材料。Springer 下载无需 API Key；项目不调用 Unpaywall。

文件能否下载取决于当前网络及实际内容权限。Springer 遇到 HTTP 401、403、429 或登录页会记录访问受限，不自动重试这些响应。Elsevier 的限流和配额处理见下文。软件不执行账号登录或验证码操作。

## 版本更新

从 1.8.0 起，软件启动约一秒后会在后台检查一次正式版本，也可点击右上角“检查更新”。发布源为 [GitHub Releases](https://github.com/YangZlXuan/paper_collections/releases)。发现比当前版本新的正式发布时，提示是否打开下载页面；自动检查期间若正在解析或下载论文，提示会延后至任务结束。软件不会自动下载安装或替换文件。

选择打开页面后，下载 Release 附件中的 `PaperDownloader.exe`，完成当前任务并关闭旧版，再替换旧 EXE。设置保存在用户目录，正常替换 EXE 可继续使用原有设置与下载结果。1.7.0 及更早版本需要先手动安装一次 1.8.0 或更高版本，之后才能收到提示。

检查使用公开 GitHub API，只发送软件版本及必要的请求头，不携带出版社 API Key、Cookie、邮箱或文献清单。网络失败、GitHub 限流或尚无公开 Release 时，自动检查不弹出错误窗口，也不影响论文下载；手动检查会说明原因。程序保持系统代理与证书配置支持，无需配置 GitHub Token。

维护者发版时，同步更新 `paper_downloader/__init__.py`、`pyproject.toml` 和 `version_info.txt`，运行构建脚本，在上述仓库创建 `vX.Y.Z` 格式标签的正式 Release，标记为 Latest 并上传对应 EXE。仅推送源码或标签不会产生更新提示；草稿和预发布版本不作为更新目标。版本按数字比较，因此 1.10.0 会正确识别为高于 1.9.0。

## Elsevier 下载与权限

API Key 首选配置在当前 Windows 用户的 **`ElsevierKey`** 环境变量中，软件通过 `os.environ` 自动读取，桌面版与命令行版共用。首次设置或修改后重新启动软件；从终端启动时也需重开终端。读取顺序为 `ElsevierKey` → 旧变量 `ELSEVIER_API_KEY` → 可选的本机加密保存值。机构令牌仍使用 `ELSEVIER_INSTTOKEN`，也可在访问设置中填写。

Elsevier 支持通过 DOI 或 PII 定位文章，ScienceDirect 链接先解析为 PII。软件使用官方 `https://api.elsevier.com/content/article/doi/{DOI}` 或 `/pii/{PII}`，先读取元数据核对文章及所选期刊，再请求 `application/pdf`。PII-only 记录可直接下载，不需要猜测 DOI。导入列表中的已知 Springer 记录不会提交给 Elsevier 队列；已知 Elsevier 记录同样不会提交给 Springer。

启用 SI 时，软件通过官方 Object Retrieval API 获取这篇论文的对象清单，从实际列出的 `mmc` 等补充材料对象逐个下载，不按编号猜测地址。普通正文图片、缩略图、公式图和作者接受稿不作为 SI。附件可能是 DOCX、PDF、ZIP、表格、数据文件或视频，不局限于 PDF。

正文及每个附件分别记录下载状态、文件大小与 SHA256；正文已下载但附件发现或下载失败时标记“部分完成”。确认无 SI 显示“无附件”，接口失败显示“发现失败”，关闭选项显示“未启用”，不会把发现失败误报为没有附件。选择同一输入和原保存目录再次运行，可校验并跳过已下载正文，补下载 SI；已通过校验的附件也会跳过。

API Key 必须具备相应的 API 访问资格、内容访问权和格式权限。网页可读全文或持有 Key 都不能保证各个 API 的权限；机构 IP 或 Elsevier 提供的 Insttoken 也可能是必需的。请使用获得授权的网络环境，并向机构或出版社确认正文及补充材料的 API 访问权限。软件不修改系统代理设置。HTTP 401 / 403 会显示权限提示，软件不会把错误页保存成 PDF 或附件。

每次 API 请求、跳转和重试均遵守至少 2 秒的间隔。程序读取响应中的配额信息；累计配额耗尽、长时间限流或全局认证权限错误时停止当前批次并保留结果，避免继续消耗请求。普通限流按服务端要求等待并有限重试，等待可以取消。解决权限问题或等待配额恢复后，使用相同输入和保存目录重新开始。

Key 和可选 Insttoken 只通过官方 API 主机的请求头发送，不放在 URL 中；跳转到其他文件主机时不携带这些凭据。日志、CSV、JSON 清单及 EXE 不包含凭据。软件兼容旧版可选的 DPAPI 本机加密保存：在访问设置中勾选记住后，密文保存在 `%LOCALAPPDATA%\PaperDownloader\elsevier.credentials.json`，仅当前 Windows 用户可解密，不随 EXE 复制到其他电脑。配置 `ElsevierKey` 后无需再使用这一保存方式，非空环境变量优先于本机保存值。

源码还提供命令行入口。配置当前用户环境变量 `ElsevierKey` 并重新打开终端后运行：

```powershell
conda run --no-capture-output -n PaperCollections python elsevier_batch_download.py --input "private/references.ris" --output "downloads/elsevier" --interval 2
```

默认同时下载正文和 SI，支持 `--no-supplements` 仅下载正文，另支持 `--journal "完整期刊名"` 和 `--column "自定义列名"`。按 Ctrl+C 取消；不要把真实 Key 写入脚本或命令行参数。

官方文档：[Article Retrieval API](https://dev.elsevier.com/documentation/ArticleRetrievalAPI.wadl)、[Object Retrieval API](https://dev.elsevier.com/documentation/ObjectRetrievalAPI.wadl)、[API 权限说明](https://dev.elsevier.com/tecdoc_article_access.html)、[配额与速率](https://dev.elsevier.com/api_key_settings.html)。

## Wiley 下载与权限

Wiley Token 保存于当前 Windows 用户的 `WileyKey` 环境变量，程序使用 `os.environ.get("WileyKey", "")` 读取。Token 不写入源代码、明文设置文件或 EXE；访问设置中的输入仅在本次运行有效。与 Elsevier 的凭据分别读取，不互相替代。

导入 DOI、含 DOI 的 RIS / BibTeX 或表格后，程序先通过 Crossref 核验 DOI、出版社与完整期刊名，再调用 `https://api.wiley.com/onlinelibrary/tdm/v1/articles/{DOI}` 下载正文。仅有 PII 的 ScienceDirect 链接不能交给 Wiley。Token 只通过 `Wiley-TDM-Client-Token` 请求头发送到 `api.wiley.com`，Crossref 查询和文件主机跳转均不携带 Token。每篇论文保存到 `wiley_` 前缀目录，正文为 `article.pdf`，另有 `manifest.json`；批次结果保存为 `manifest_wiley_*.csv`。

Wiley 官方同时限制最多 3 篇/秒和每 10 分钟 60 次请求，持续批次需至少 10 秒间隔。程序对请求、跳转及重试统一限速；429 按服务端要求等待，长时间限流会暂停批次并保留结果。再次选择同一输入和目录，会核验已有 PDF 的大小和 SHA256 后跳过。

Wiley TDM API 仅提供正文 PDF，**不提供 SI**。1.7.0 的 SI 获取独立使用 Crossref 中已登记的组件和补充材料关系：只处理能与目标论文对应的真实线索，不从 DOI 猜测附件文件名，不把正文链接、参考文献、普通图片或表格当成 SI。找到可用文件地址后逐个下载，核验非空、传输大小、SHA256，PDF 和 ZIP/Office 还执行相应格式检查。SI 请求不携带 Wiley Token，不自动操作 Wiley 网页验证。

Crossref 不保存完整的附件集合，有 SI 的论文也可能没有登记附件。结果显示“未检索到SI记录”时，不能据此断言论文没有附件；有线索但没有可直接获取的文件时显示“需手动获取”，查询异常显示“发现失败”。已完成正文但 SI 尚未完成时显示“部分完成”，正文保留；每篇 `manifest.json` 保留发现来源、附件线索和逐项结果，用户可打开文章页面进一步核对。相同输入、相同目录重跑可跳过已验证正文并重新查询 SI，也会复用已通过哈希校验的附件。关闭补充材料选项即可恢复仅正文模式。

订阅文献访问依赖机构 IP；网页登录或 SSO 权限不能替代 API 的 IP 访问条件。Token 有效也不保证每篇论文均可下载；404 可能表示无订阅或网络权限，不能简单理解为 DOI 不存在。软件不会修改系统代理设置。

命令行入口：

```powershell
conda run --no-capture-output -n PaperCollections python wiley_batch_download.py --input "private/references.ris" --output "downloads/wiley" --interval 10
```

默认查询 SI，可用 `--no-supplements` 仅下载正文；另支持 `--journal "完整期刊名"`、`--column "自定义列名"` 和 Ctrl+C 取消。`examples/wiley_dois.txt` 使用 Wiley 官方客户端示例 DOI，实际下载以当前网络及权限为准。

官方参考：[Wiley TDM 文档及协议](https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining)、[Token 申请入口](https://static.wiley.com/tdm/)、[Wiley 官方 Python 客户端](https://github.com/WileyLabs/tdm-client)、[Crossref 组件](https://www.crossref.org/documentation/schema-library/markup-guide-record-types/components/)、[Crossref 关联关系](https://www.crossref.org/documentation/schema-library/markup-guide-metadata-segments/relationships/)。本软件沿用 `requests` 实现，以统一现有界面的结果清单、取消和校验流程；无需额外安装 `wiley-tdm`。

## 文献导入格式

- **TXT / 粘贴文本**：每行一个 DOI、DOI 链接或 ScienceDirect 文章链接，也可以从一行中提取多个标识。
- **CSV / TSV**：自动识别 `DOI`、`Item DOI`、`DI` 及 URL / PII 等标识列；自定义列名可指定只读取该列。没有可识别表头时扫描单元格并在日志提示。
- **XLSX**：读取全部工作表，按工作表识别标识列。指定列名时跳过没有该列的工作表；所有工作表均找不到时提示错误。
- **RIS**：读取每条引文的 DOI、文章链接、PII、标题和期刊；支持多行字段。不把摘要和参考文献中提到的其他 DOI 当作待下载论文。
- **BIB / BIBTEX**：解析 BibTeX 引文的 DOI、URL、PII、标题和期刊字段，支持嵌套花括号、多行字段和字符串宏。
- 支持 UTF-8、UTF-8 BOM、GBK / GB18030 文本及带 BOM 的 UTF-16。按 DOI / PII 去重并合并可关联记录中的信息，保留首次出现的顺序。缺少可用标识的引文记录会计入无效记录提示。
- DOI 与 PII 对应关系冲突，或 BibTeX 条目损坏、未闭合时会明确报错，请修正源文件后重新导入。
- 旧版 `.xls` 请先另存为 `.xlsx` 或 CSV。

ScienceDirect 文章链接通常包含 `/science/article/pii/` 或 `/science/article/abs/pii/`。软件提取其中的 PII、清理查询参数并保留标准文章链接；不联网查询或猜测 DOI。搜索页、期刊首页和非 ScienceDirect 域名不会作为文章链接导入。

在 ScienceDirect 检索并勾选论文后，可通过 **Export → RIS / BibTeX** 导出，再导入本软件并选择 Elsevier。仅有 PII 的文章也可以进入下载队列。导出 CSV 包含 DOI、PII、URL、标题、期刊，便于整理和再次导入。解析文件本身不联网，点击开始下载后才调用 API。

`examples/dois.txt` 和 `examples/dois.csv` 提供 DOI 格式示例；`examples/references.ris`、`examples/references.bib` 和 `examples/sciencedirect_links.txt` 提供引文及链接示例。ScienceDirect 示例 PII 是虚构的，仅用于验证导入；`examples/elsevier_dois.txt` 是一篇真实 Cell 论文的 DOI，实际可下载范围以 API 权限为准。

期刊选择会与出版社返回的期刊元数据比较，忽略大小写、空格和标点，不做标题子串匹配。指定期刊但缺少对应元数据时会跳过并记录原因。“全部期刊”不局限于下拉列表；返回的文章标识与请求不一致时会报错。

## Conda 环境

本项目使用名为 **PaperCollections** 的独立 Conda 环境。在 PyCharm 中选择该环境对应的 Python 解释器，安装路径以本机 Conda 配置为准。

创建环境并运行：

```powershell
conda env create -f environment.yml
conda activate PaperCollections
python app.py
```

环境已存在时安装依赖：

```powershell
conda run -n PaperCollections python -m pip install -r requirements-build.txt
```

依赖包括 `requests`（网络访问）、`beautifulsoup4`（解析论文网页）、`openpyxl`（读取 XLSX）、`bibtexparser`（解析 BibTeX）、`tk` / `tkinter`（图形界面）和 `PyInstaller`（打包），均安装在该 Conda 环境内。项目不使用系统 Python 环境。也可双击 `run.bat` 启动源码版，前提是终端能找到 `conda` 命令。

## 测试与打包

```powershell
conda run --no-capture-output -n PaperCollections python -m unittest discover -s tests -v
conda run --no-capture-output -n PaperCollections python build.py
```

旧 EXE 正在运行时，可构建到版本目录：`conda run --no-capture-output -n PaperCollections python build.py --dist-dir dist/v1.8.0`。

或者双击 `build.bat`。构建脚本检查当前 Conda 环境名称，安装所需依赖，运行测试，再生成单文件、无控制台窗口的 EXE，并执行离线启动自检。

- 可执行文件：`dist/PaperDownloader.exe`
- EXE 自检报告：`build/exe-self-test.json`
- 独立离线自检：`dist\PaperDownloader.exe --self-test build\check.json`

离线测试使用模拟出版社网页和文件数据，覆盖期刊筛选、正文与补充文件保存、Cookie 作用域、逐跳检查、限速、重试、取消及 UI 状态流转。实际下载成功率取决于网站返回内容和访问权限。

1.8.0 已通过 246 项自动化测试及 EXE 离线自检。更新检查测试覆盖版本数字排序、正式发布与页面地址校验、网络失败、响应大小限制、凭据隔离、后台线程、任务期间延后提示和关闭窗口后的回调处理；测试与 EXE 自检不访问更新网络接口。

1.7.0 已通过 230 项自动化测试，覆盖 WileyKey / ElsevierKey 环境变量读取、出版社 API 下载、DOI / PII 核验、期刊筛选、权限拒绝、配额暂停、限速、重试、PDF 校验和复用、跳转时隔离凭据、加密保存、GUI 与命令行流程，以及原有 Springer 和导入功能。Wiley SI 测试另外覆盖 Crossref 组件与正反向关系、父 DOI 归属、普通图片及关联论文排除、真实附件地址格式、查询不完整提示、未登记与人工处理状态、文件校验、取消和复用。EXE 离线自检检查 Wiley 默认 10 秒及 Crossref SI 配置、新解析模块、Elsevier SI 默认配置、DPAPI 加密解密和引文解析，确认依赖随程序打包。

联网验证需要使用有权访问的文献与网络环境。公开仓库保留模拟测试和通用示例；个人下载记录、原始响应、文献文件及验证日志应保存在本地私有目录。

## 输出和运行细节

所选目录下按 DOI 和固定短哈希创建子目录，正文保存为 `article.pdf`，补充材料保存为 `SI_<地址哈希>.<扩展名>`，因此页面链接次序变化不会将旧文件错认成另一个附件。每篇论文的 `manifest.json` 记录文件地址、状态、大小和 SHA256 校验值。每次任务另生成独立的 `manifest_日期时间_随机后缀.csv`，每处理一篇立即写入，重复运行不会覆盖旧的批次清单。

Elsevier 的逐篇目录使用 `elsevier_` 前缀，以导入时的 DOI 或 PII 标识命名；补充文件命名为 `SI_<附件ref>_<地址哈希>.<扩展名>`，批次清单使用 `manifest_elsevier_` 前缀。PII 查询获得 DOI 后，清单同时保存两者和原始 `reference_key`，界面仍更新同一条记录。继续下载时请保持相同的输入标识和保存目录。

| CSV 状态 | 含义 |
| --- | --- |
| `success` | 正文及启用下载的补充文件处理成功 |
| `exists` | 目标文件已存在且通过校验，跳过重复下载 |
| `partial` | 部分文件已保存，但正文或部分补充文件未成功 |
| `no_pdf` | 页面没有找到正文 PDF，且未保存可用补充文件 |
| `filtered` | 期刊不匹配，或缺少所选期刊的元数据 |
| `access_denied` | 网站要求登录、权限不足或限制访问 |
| `failed` | 网页、网络、文件校验或保存失败，原因见 `error` |
| `cancelled` | 当前论文被取消 |

CSV 包含 DOI、PII、原始记录标识、状态、期刊、标题、论文页面、PDF 地址、保存路径、补充文件数量、逐篇 JSON 路径和错误原因。取消或暂停时尚未开始的文献不写入批次清单，界面标为未处理。确认论文没有补充材料是正常情况，不单独算作失败。

下载按顺序执行，用户设置的间隔适用于每次网络请求，包括文章页面、元数据、跳转、正文、补充文件和重试。Springer / Elsevier 默认及最低间隔 2 秒，Wiley 为 10 秒。间隔会自动记住，下次启动沿用；下载期间不可更改。临时网络故障最多重试 2 次。取消会在收到下一数据块、等待间隔或网络超时后生效，单次请求默认读取超时 90 秒。单个文件上限 150 MB。

文件保存使用临时文件并计算 SHA256，失败时清理临时文件。PDF 检查 `%PDF-` 文件头和 `%%EOF` 结尾标记，其他附件检查非空且不是 HTML 页面。Elsevier 附件还核对官方目录提供的大小，拒绝 API 错误或元数据响应；ZIP/Office 文件逐项校验压缩数据及 CRC，Office 包检查必需内容，常见图片检查文件头。合法 XML/JSON 数据附件不会仅因格式而被拒绝。ZIP 校验不解压落盘，校验总展开大小上限 500 MB，最多 10,000 个条目，加密 ZIP 会明确报校验失败。已有正文在存在原校验值时核对大小和哈希；已有补充材料通过原清单中的地址、大小和哈希校验后才跳过。PDF 检查是传输完整性的基础检查，不是完整结构验证；重复运行仍需在线获取文章元数据和附件目录。

Springer 请求只访问 HTTPS 的 `nature.com`、`springer.com`、`springernature.com` 及其子域；文件还允许精确的官方 CDN 主机 `static-content.springer-cdn.com`。Elsevier API 请求只访问 `api.elsevier.com`，PDF 跳转仅允许代码中列出的官方文件主机。Wiley 查询 `api.crossref.org` 和 `api.wiley.com`，正文允许精确的 `onlinelibrary.wiley.com`、`alm.wiley.com`；ALM 临时下载路径不写入清单。所有重定向逐跳检查；Elsevier / Wiley 不使用浏览器 Cookie，API 凭据不随外站跳转发送。Springer Cookie 按原有域名、路径、Secure 和有效期限制使用。

可选的 Cookie 文件应为 Netscape 格式。可以通过界面选择，也可用环境变量 `SN_COOKIE_FILE` 指定。邮箱、出版社、期刊、列名、保存路径、下载间隔、补充材料选项和 Cookie 文件路径保存在 `%LOCALAPPDATA%\PaperDownloader\settings.json`，Cookie 内容不会写入配置、日志或项目文件；启动错误日志在同目录 `crash.log`。

1.0 版生成的平铺 PDF 和历史清单不会被自动搬移；1.1 版使用上述逐篇目录结构。

## 项目结构与期刊目录

```text
app.py                       程序入口与 EXE 离线自检
elsevier_batch_download.py    Elsevier 命令行批量下载入口
wiley_batch_download.py       Wiley 正文与 Crossref SI 命令行入口
paper_downloader/
  gui.py                     Tkinter 图形界面与后台任务协调
  catalog.py                 出版社与期刊目录读取
  doi_parser.py              DOI / RIS / BibTeX / ScienceDirect 链接解析与去重
  downloader.py              Springer / Nature 正文和补充材料下载引擎
  elsevier.py                Elsevier 官方 API 元数据、正文 PDF 与 SI 下载
  wiley.py                   Wiley TDM 正文下载与 Crossref 期刊核验
  wiley_supplements.py        Crossref SI 发现、附件下载及校验
  credentials.py             ElsevierKey / WileyKey 环境变量读取与 Elsevier 可选 DPAPI 存储
  settings.py                用户设置保存
  updates.py                 GitHub 正式版本检查与发布地址校验
  data/journals.json         内置期刊目录
tests/                       离线自动化测试
tools/extract_catalog.py     从原始分类表重新生成目录
environment.yml              PaperCollections Conda 配置
requirements*.txt            运行与打包依赖
PaperDownloader.spec         PyInstaller 单文件打包配置
```

内置目录包含 108 本期刊、13 个出版社；其中 Springer Nature 15 本、Elsevier 41 本、Wiley 17 本，均已启用下载。“其他出版机构”按其明确标注的出版社拆分。公开 JSON 仅保留目录版本、出版社标识、名称、启用状态和期刊名称，不包含来源工作簿文件名、路径、哈希、工作表名称或原表行号。生成脚本仅读取分类表中的出版社与期刊对应关系，备注和下载日志不会写入目录或作为指令执行。运行 EXE 不依赖原 Excel 文件。

若期刊表后续更新，可使用 `tools/extract_catalog.py --help` 查看重新提取方式，更新后重新打包。增加出版社下载能力时扩展下载适配及测试，并启用目录中对应出版社的 `enabled` 标记。

## 源码与发行包的隐私约定

- API Key、Token 和 Cookie 由使用者在本机配置，不应写入源码、示例或提交记录。`.env.example` 如需提供，只填写变量名称和虚构占位值。
- 原始文献表和私人资料放在 `private/` 或 `local-data/`，下载输出放在 `downloads/`。这些目录与 `build/`、`dist/`、本地设置、凭据备份、日志和常见 Cookie 文件已加入 Git 忽略规则。自定义文件名或其他输出目录仍需自行检查。
- `%LOCALAPPDATA%\PaperDownloader` 中的设置、凭据和日志仅供本机使用，不复制进仓库或发行包。联系邮箱为可选项；填写后会随 Springer 请求的 User-Agent 发送给允许访问的出版社或 CDN。
- 发布源码前运行 `git status --short --untracked-files=all` 和 `git diff --cached` 检查待提交内容。`.gitignore` 不会清除已跟踪文件或历史记录，也不会对手工压缩整个目录进行过滤。
- EXE 通过 GitHub Release 单独上传，使用清理后的源码重新构建。不要把整个项目、`build/`、下载目录或本机配置打包发布。内置目录修改后，既有 EXE 不会自动改变，必须重新打包。
