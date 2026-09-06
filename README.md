# TOC Radar

> Regular feeds of top political theory journals.

政治理论期刊目录(TOC)分级订阅系统。每天从 Crossref 拉取配置刊物的新条目,
按研究关键词与作者 watchlist 打分,产出一个带优先级标注的看板、一个 Atom feed
和一封周报邮件。

## 它做什么

- **分级订阅**:T1 核心刊每期全目录入库;T2 相关刊只收关键词/作者命中的条目;
  T3 泛读刊只留每月 top-N。
- **优先级标注**:每条算一个分数,分 `必读 / 推荐 / 浏览` 三档,并保留
  `why` 记录——哪个词、在标题还是摘要里命中、加了多少分,都能在界面上展开看。
- **预印去重**:同一 DOI 先以 online-first 出现、后来才有卷期号,只算一条,
  不会推你两遍。

## 结构

```
config/journals.yaml    刊物清单与层级(改这里增删刊)
config/keywords.yaml    关键词、作者 watchlist、权重、阈值(改这里调排序)
src/harvest.py          Crossref 增量抓取 + online-first→in-issue 状态机
src/score.py            打分与分级门控
src/render.py           生成 site/data.json 与 site/feed.xml
src/mail.py             周报邮件
site/index.html         看板(静态单页,无框架)
tests/test_pipeline.py  离线测试,不需要网络
```

## 本地跑

```bash
pip install -r requirements.txt

python src/harvest.py --validate     # 先校验 ISSN 都能解析
python src/harvest.py --days 120     # 首次抓取
python src/score.py
python src/score.py --histogram      # 看分数分布,据此定阈值
python src/render.py
python src/mail.py --dry-run         # 生成 digest-preview.html

python -m http.server 8000 --directory site
```

## 部署

GitHub Actions 每天 06:00(Asia/Shanghai)跑一次 `harvest.yml`,把数据 commit
回仓库并部署 Pages。`weekly-mail.yml` 每周一 08:00 发周报。

需要配置:

**Repository variables**
| 名称 | 值 |
|---|---|
| `SITE_URL` | `https://<user>.github.io/<repo>` |

**Repository secrets**
| 名称 | 说明 |
|---|---|
| `CROSSREF_MAILTO` | 你的邮箱。Crossref 的 polite pool 靠它给更高配额,建议填 |
| `SMTP_HOST` | 如 `smtp.gmail.com` 或 `smtp.resend.com` |
| `SMTP_PORT` | `587`(STARTTLS)或 `465`(SSL) |
| `SMTP_USER` / `SMTP_PASS` | Gmail 用应用专用密码;Resend 用户名填 `resend`,密码填 API key |
| `MAIL_FROM` / `MAIL_TO` | 发件人 / 收件人 |

邮件相关的 secret 不填也不会报错,`mail.py` 会跳过发送。

Settings → Pages → Source 选 **GitHub Actions**。

## 调优

1. 首跑后在 Actions 日志里看 `--histogram` 输出的分数分布。
2. 按它建议的 p95 / p75 改 `config/keywords.yaml` 的 `thresholds`。
3. 看板的「权重」标签页可以拖滑块本地预览不同阈值的分档结果,定好了再写回配置。

关键词权重同理:`repeat_hit_factor` 控制同组内重复命中的衰减,
`group_cap_factor` 控制单组贡献上限——这两个是防止「一篇泛泛谈马克思的文章
靠堆词冲上必读」的旋钮。

## 已知边界

- Crossref 对部分刊物有滞后(数天到数周),`New Left Review`、`Radical Philosophy`
  这类覆盖较弱,可能需要另配 RSS。
- 不在 Crossref 的 OA / 法语小刊(Décalages、Cahiers du GRM、Actuel Marx、
  Multitudes)需要各写一个 scraper,本期未做。
- 摘要不是每家出版商都给。没有摘要的条目只能靠标题打分,界面上标了「无摘要」。
