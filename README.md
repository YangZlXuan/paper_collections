# 论文批量下载助手

**[下载最新版 Windows 64 位程序](https://github.com/YangZlXuan/paper_collections/releases/latest/download/PaperDownloader.exe)** · [查看发布说明](https://github.com/YangZlXuan/paper_collections/releases/latest)

用 DOI 清单、引文文件或关键词查找论文，批量保存正文 PDF 和可用的补充材料（SI）。目前支持 **Springer Nature（含 Nature）、Elsevier（ScienceDirect）和 Wiley** 的检索与下载；其他出版社的文献可以导入、预览和导出清单。

## 下载与启动

点击上方下载链接，双击 `PaperDownloader.exe` 即可运行，无需安装 Python 或 Conda。如果从发布页下载，请展开 **Assets**，选择 `PaperDownloader.exe`；`Source code (zip)` 是源码包。

以后可在软件右上角点击 **检查更新**。如有新版，先完成当前下载，关闭旧版，再用新下载的 EXE 替换旧文件。

## 我需要申请哪种 API？

只需配置你要使用的出版社。**导入已有文献、预览列表和导出 CSV 都不需要密钥。**

| 出版社 | 按关键词检索 | 按已有 DOI 下载正文 |
| --- | --- | --- |
| Springer Nature | 需要 Springer Meta Key | 不需要 API Key；能否下载取决于当前网络和文章访问权限 |
| Elsevier | 需要已开通 ScienceDirect Search 权限的 Elsevier API Key | 使用同一把 Key，但还需要正文 PDF 权限；下载补充材料另需附件访问权限。也支持 ScienceDirect 文章链接 |
| Wiley | 不需要 Token | 需要 Wiley TDM Token；下载订阅文章时还需在有权限的机构网络下使用 |

### Springer Nature：申请关键词检索用的 Meta Key

1. 打开 [Springer Nature 开发者门户](https://dev.springernature.com/)，注册或登录账号。
2. 登录后进入 **API Management → API Keys**，获取可用于 **Meta API** 的密钥。[免费方案](https://dev.springernature.com/subscription/)包含 Meta API，足以使用本软件的基础关键词检索。
3. 在软件中打开 **API / Cookie 设置**，把密钥粘贴到 **Springer Meta Key**，点击 **完成**。

如果手头已有 Springer 或 Nature 论文的 DOI，可以跳过以上步骤直接导入。下载订阅论文时，请先确认机构授予的访问权限；如需要使用已有的授权会话，可以在同一窗口的 **Springer Cookie** 中选择 Netscape 格式的 Cookie 文件。

### Elsevier：申请 ScienceDirect API Key

1. 打开 [Elsevier 开发者门户](https://dev.elsevier.com/index.jsp)，点击 **I want an API Key**，注册或登录 Elsevier 账号，按页面提示申请并复制 API Key。
2. 在软件的 **API / Cookie 设置 → Elsevier API Key** 中粘贴密钥，点击 **完成**。若希望下次继续使用，可勾选 **在本机加密保存 Elsevier 凭据**。
3. 新 Key 不会自动获得 ScienceDirect 内容权限。若检索或下载提示访问受限，请向机构图书馆或 [Elsevier API 支持](https://dev.elsevier.com/support.html)说明需要的功能：关键词检索对应 **ScienceDirect Search**，正文 PDF 对应 **Article Retrieval**，补充材料对应 **Object Retrieval**。有些机构通过校园网 IP 识别访问资格；如机构或 Elsevier 提供了 **Insttoken**，将它填入同一窗口的 **Insttoken（可选）**，否则留空。

### Wiley：申请下载正文用的 TDM Token

1. 打开 [Wiley Token 申请页](https://static.wiley.com/tdm/)，登录或注册 Wiley Online Library 账号。也可从 [Wiley 文本与数据挖掘说明](https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining)中的 **Get a Text and Data Mining Token** 进入。
2. 确认适用的机构协议，阅读并接受 Wiley 的 TDM 条款，然后复制页面显示的 Token。
3. 在软件的 **API / Cookie 设置 → Wiley API Token** 中粘贴 Token，点击 **完成**。如果只是检索文献或导出清单，可以跳过申请。

**希望每次启动都自动读取密钥？** Springer 和 Wiley 在界面中填写的值仅本次有效。Windows 用户可在开始菜单搜索“编辑帐户的环境变量”，在自己的 **用户变量** 中新建对应项：变量名用下表的名称，变量值粘贴自己的密钥。重新启动软件后生效；从终端启动的，也要重新打开终端。请勿把密钥、Token 或 Cookie 发到公开问题或截图中。

| 变量名 | 变量值 |
| --- | --- |
| `SpringerMetaKey` | Springer Meta API Key |
| `ElsevierKey` | Elsevier API Key |
| `ELSEVIER_INSTTOKEN` | Elsevier 机构令牌（仅在机构提供时填写） |
| `WileyKey` | Wiley TDM Token |

## 第一次下载

1. **选择出版社和期刊。** 如果清单里有多种期刊，保留 **全部期刊（该出版社）**；只要某一本时，在期刊框输入名称，然后从下拉列表选中完整刊名。
2. **添加文献。** 以下三种方式任选一种。导入会替换当前列表；想保留上一份清单时，先点 **导出解析列表 CSV**。

   | 手头有什么 | 怎么操作 |
   | --- | --- |
   | DOI 或 ScienceDirect 文章链接 | 点击 **粘贴 DOI / 链接**，每行粘贴一条，再点 **解析并导入**。例如，选择 Springer Nature 后可粘贴 `10.1007/s12274-024-6813-0` 试导入。ScienceDirect 文章链接请选择 Elsevier。 |
   | 文献文件 | 点击 **选择文件**，软件会自动解析并去重。支持 TXT、CSV、TSV、XLSX、RIS 和 BibTeX（.bib / .bibtex）；旧版 .xls 请先另存为 .xlsx 或 CSV。若需指定表格列，在 **DOI 列名（可选）** 中填写表头，再点 **解析文件**。可参考 [CSV 样例](examples/dois.csv) 或 [RIS 样例](examples/references.ris)。 |
   | 关键词 | 点击 **关键词检索**，输入检索词，可限定发表年份和篇数（默认 100 篇，范围 1–1000），再点 **开始检索**。在 **结果期刊** 中可筛选本次已获取的结果，选好论文后点 **导入选中**。 |

3. **开始下载。** 在 **论文列表** 核对记录，用 **选择目录** 指定保存位置。默认同时下载补充材料；只要正文时，取消勾选 **下载补充材料**。下载间隔可保留默认值（Springer / Elsevier 为 2 秒，Wiley 为 10 秒）。点击 **开始下载** 后才会保存文件。
4. **查看结果。** 双击已下载的记录可打开 PDF；选中一条记录可查看失败原因或点击 **打开论文文件夹** 查看附件。**打开结果清单** 可查看本批次的 CSV，列有每篇论文的状态、保存路径和错误原因。

## 遇到问题时

- **能搜到论文，下载却显示“访问受限”**：检索到标题不代表能下载全文。先用 **打开文章页面** 核对订阅与机构网络；对于 Elsevier 和 Wiley，还要分别确认上面列出的 API 权限或 TDM 授权。
- **Wiley 检索失败或找不到附件**：若检索遇到验证页，可以从 Wiley 网站导出 DOI 或 RIS，用 **选择文件** 导入。Wiley TDM 只提供正文；软件会另外通过 Crossref 查找已登记的附件。“未检索到SI记录”不代表论文一定没有附件，可用 **打开文章页面** 核对 Supporting Information。
- **中途取消、限流或仅“部分完成”**：已保存的文件会保留。若提示限流或配额用尽，等出版社允许继续访问后，用相同的 DOI 或文章链接和原保存目录重新开始；软件会跳过已验证的文件，并尝试补齐缺失项。

## 可选：从源码运行

已有 Conda 的用户也可以从[发布页](https://github.com/YangZlXuan/paper_collections/releases/latest)下载 **Source code (zip)**。解压后，在含有 `environment.yml` 的目录打开 Conda 终端，依次运行：

```shell
conda env create -f environment.yml
conda activate PaperCollections
python app.py
```

如需命令行操作，在已激活的环境中运行 `python keyword_batch_download.py --help` 查看检索与导出用法；按已有文献文件批量下载可查看 `python elsevier_batch_download.py --help` 或 `python wiley_batch_download.py --help`。
