OPERATOR_API_VERSION = 2
SLOT = "report"


def _escape(value):
    if value is None:
        return "—"
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace(chr(34), "&quot;")
        .replace(chr(39), "&#x27;")
    )


def _coord(value):
    return "%.2f" % float(value)


def _number(value):
    if value is None:
        return "Unavailable"
    return "%.2f" % float(value)


def _money(value, zh):
    if value is None:
        return "不可用" if zh else "Unavailable"
    return ("¥" if zh else "CNY ") + ("{:,.2f}".format(float(value)))


def _percent(value, zh):
    if value is None:
        return "不可用" if zh else "Unavailable"
    return ("%+.2f" % (float(value) * 100.0)) + "%"


def _field_map(payload):
    result = {}
    sections = payload["sections"]
    for section_index in range(len(sections)):
        fields = sections[section_index]["fields"]
        for field_index in range(len(fields)):
            field = fields[field_index]
            result[field["field_id"]] = field
    return result


def _raw(fields, field_id):
    field = fields.get(field_id)
    if type(field) is not dict or field.get("availability") != "AVAILABLE":
        return None
    return field.get("raw")


def _text(zh, chinese, english):
    return chinese if zh else english


def _x(index, count, left, width):
    if count <= 1:
        return left + width / 2.0
    return left + width * float(index) / float(count - 1)


def _y(value, low, high, top, height):
    if high <= low:
        return top + height / 2.0
    return top + height * (high - float(value)) / (high - low)


def _series_path(values, low, high, left, top, width, height):
    commands = []
    count = len(values)
    for index in range(count):
        command = "M" if index == 0 else "L"
        commands.append(
            command
            + _coord(_x(index, count, left, width))
            + " "
            + _coord(_y(values[index], low, high, top, height))
        )
    return " ".join(commands)


def _axis(rows, low, high, left, top, width, height, value_suffix):
    bottom = top + height
    parts = [
        '<line class="axis" x1="'
        + _coord(left)
        + '" y1="'
        + _coord(bottom)
        + '" x2="'
        + _coord(left + width)
        + '" y2="'
        + _coord(bottom)
        + '"/>',
        '<line class="axis" x1="'
        + _coord(left)
        + '" y1="'
        + _coord(top)
        + '" x2="'
        + _coord(left)
        + '" y2="'
        + _coord(bottom)
        + '"/>',
        '<text class="axis-label" x="'
        + _coord(left - 8)
        + '" y="'
        + _coord(top + 5)
        + '" text-anchor="end">'
        + _escape(("%.2f" % high) + value_suffix)
        + "</text>",
        '<text class="axis-label" x="'
        + _coord(left - 8)
        + '" y="'
        + _coord(bottom)
        + '" text-anchor="end">'
        + _escape(("%.2f" % low) + value_suffix)
        + "</text>",
    ]
    count = len(rows)
    if count:
        indices = [0]
        middle = count // 2
        if middle != 0 and middle != count - 1:
            indices.append(middle)
        if count > 1:
            indices.append(count - 1)
        for item_index in range(len(indices)):
            index = indices[item_index]
            date_value = rows[index].get("date", rows[index].get("Date", ""))
            parts.append(
                '<text class="axis-label date-label" x="'
                + _coord(_x(index, count, left, width))
                + '" y="'
                + _coord(bottom + 22)
                + '" text-anchor="middle">'
                + _escape(date_value)
                + "</text>"
            )
    return "".join(parts)


def _date_index(rows, date_value):
    for index in range(len(rows)):
        if rows[index].get("date") == date_value:
            return index
    return -1


def _holding_intervals(rows, holdings):
    intervals = []
    start = None
    limit = min(len(rows), len(holdings))
    for index in range(limit):
        held = int(holdings[index].get("position_after", 0)) != 0
        if held and start is None:
            start = index
        if not held and start is not None:
            intervals.append([start, index - 1])
            start = None
    if start is not None:
        intervals.append([start, limit - 1])
    return intervals


def _price_chart(rows, events, holdings, zh):
    title = _text(zh, "价格、买卖点与持仓区间", "Price, actions, and holding intervals")
    if not rows:
        return (
            '<section class="chart-card"><h2>'
            + title
            + '</h2><div class="chart-empty">'
            + _text(zh, "价格序列不可用", "Price series unavailable")
            + "</div></section>"
        )
    left = 62.0
    top = 28.0
    plot_width = 838.0
    plot_height = 242.0
    values = []
    for index in range(len(rows)):
        values.append(float(rows[index].get("close", rows[index].get("price"))))
    domain_values = list(values)
    for index in range(len(events)):
        domain_values.append(float(events[index].get("price")))
    low = min(domain_values)
    high = max(domain_values)
    padding = (high - low) * 0.08 if high > low else max(abs(high) * 0.02, 1.0)
    low -= padding
    high += padding
    buy_count = 0
    sell_count = 0
    for index in range(len(events)):
        if events[index].get("side") == "BUY":
            buy_count += 1
        elif events[index].get("side") == "SELL":
            sell_count += 1
    parts = [
        '<section class="chart-card"><div class="chart-heading"><h2>'
        + title
        + "</h2><p>"
        + _text(zh, "▲ BUY 买入；■ SELL 卖出；斜纹为持仓。", "▲ BUY; ■ SELL; hatched areas are holding intervals.")
        + '</p></div><svg class="chart chart-price" role="img" aria-labelledby="price-title price-desc" '
        + 'viewBox="0 0 920 330" data-point-count="'
        + str(len(rows))
        + '" data-event-count="'
        + str(len(events))
        + '" data-buy-count="'
        + str(buy_count)
        + '" data-sell-count="'
        + str(sell_count)
        + '"><title id="price-title">'
        + title
        + '</title><desc id="price-desc">'
        + _text(zh, "完整价格路径、每个 BUY/SELL 标记及持仓区间；下方台账提供等价明细。", "Complete price path with every BUY/SELL marker and holding interval; equivalent details follow in tables.")
        + '</desc><defs><pattern id="price-holding-pattern" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="8" class="holding-hatch"/></pattern></defs>',
    ]
    intervals = _holding_intervals(rows, holdings)
    step = plot_width / float(max(len(rows) - 1, 1))
    for interval_index in range(len(intervals)):
        start = intervals[interval_index][0]
        end = intervals[interval_index][1]
        start_x = max(left, _x(start, len(rows), left, plot_width) - step / 2.0)
        end_x = min(left + plot_width, _x(end, len(rows), left, plot_width) + step / 2.0)
        parts.append(
            '<rect class="holding-interval" data-start-index="'
            + str(start)
            + '" data-end-index="'
            + str(end)
            + '" data-start-date="'
            + _escape(rows[start].get("date"))
            + '" data-end-date="'
            + _escape(rows[end].get("date"))
            + '" x="'
            + _coord(start_x)
            + '" y="'
            + _coord(top)
            + '" width="'
            + _coord(max(end_x - start_x, 2.0))
            + '" height="'
            + _coord(plot_height)
            + '" fill="url(#price-holding-pattern)"><title>'
            + _escape(rows[start].get("date"))
            + " — "
            + _escape(rows[end].get("date"))
            + "</title></rect>"
        )
    parts.append(
        '<path class="series price-series" data-series="close" d="'
        + _series_path(values, low, high, left, top, plot_width, plot_height)
        + '"/>'
    )
    for event_index in range(len(events)):
        event = events[event_index]
        side = event.get("side")
        point_index = _date_index(rows, event.get("Date"))
        if point_index >= 0:
            placement = "mapped"
            x_value = _x(point_index, len(rows), left, plot_width)
            y_value = _y(float(event.get("price")), low, high, top, plot_height)
        else:
            placement = "unmapped"
            x_value = left + plot_width * float(event_index + 1) / float(len(events) + 1)
            y_value = top + plot_height
        css_side = "buy" if side == "BUY" else "sell"
        parts.append(
            '<g class="event-marker '
            + css_side
            + '" data-event-index="'
            + str(event_index)
            + '" data-point-index="'
            + str(point_index)
            + '" data-date="'
            + _escape(event.get("Date"))
            + '" data-side="'
            + _escape(side)
            + '" data-placement="'
            + placement
            + '" transform="translate('
            + _coord(x_value)
            + " "
            + _coord(y_value)
            + ')"><title>'
            + _escape(side)
            + " · "
            + _escape(event.get("Date"))
            + " · "
            + _escape(event.get("price"))
            + " · "
            + _escape(event.get("reason"))
            + "</title>"
        )
        if side == "BUY":
            parts.append('<polygon points="0,-10 -8,7 8,7"/><text x="0" y="-14" text-anchor="middle">BUY ▲</text>')
        else:
            parts.append('<rect x="-7" y="-7" width="14" height="14"/><text x="0" y="-13" text-anchor="middle">SELL ■</text>')
        if placement == "unmapped":
            parts.append(
                '<text class="unmapped-label" x="0" y="22" text-anchor="middle">'
                + _text(zh, "事件日期未匹配 / Unmatched event date", "Unmatched event date")
                + "</text>"
            )
        parts.append("</g>")
    parts.append(_axis(rows, low, high, left, top, plot_width, plot_height, ""))
    parts.append("</svg></section>")
    return "".join(parts)


def _equity_chart(rows, performance, zh):
    title = _text(zh, "策略净值与可用参考", "Strategy equity and available reference")
    if not rows:
        return '<section class="chart-card"><h2>' + title + '</h2><div class="chart-empty">' + _text(zh, "净值序列不可用", "Equity series unavailable") + "</div></section>"
    left = 62.0
    top = 28.0
    plot_width = 838.0
    plot_height = 242.0
    equities = []
    for index in range(len(rows)):
        equities.append(float(rows[index].get("equity")))
    first_equity = equities[0]
    strategy = []
    for index in range(len(equities)):
        strategy.append(equities[index] / first_equity)
    reference_available = type(performance) is dict and performance.get("buy_and_hold_return") is not None
    reference = []
    if reference_available:
        first_close = float(rows[0].get("close"))
        if first_close <= 0:
            reference_available = False
        else:
            for index in range(len(rows)):
                reference.append(float(rows[index].get("close")) / first_close)
    all_values = list(strategy)
    if reference_available:
        all_values += reference
    low = min(all_values)
    high = max(all_values)
    padding = (high - low) * 0.08 if high > low else 0.02
    low -= padding
    high += padding
    parts = [
        '<section class="chart-card"><div class="chart-heading"><h2>'
        + title
        + "</h2><p>"
        + _text(zh, "策略净值以首日为 1.00；参考仅在来源字段合格可用时显示。", "Strategy equity starts at 1.00; a reference is shown only when its source field is available.")
        + '</p></div><svg class="chart chart-equity" role="img" aria-labelledby="equity-title equity-desc" viewBox="0 0 920 330" data-point-count="'
        + str(len(rows))
        + '" data-reference-count="'
        + ("1" if reference_available else "0")
        + '"><title id="equity-title">'
        + title
        + '</title><desc id="equity-desc">'
        + _text(zh, "归一化策略净值与可用的同窗收盘参考。", "Normalized strategy equity and the available same-window close reference.")
        + '</desc><path class="series equity-series" data-series="strategy-equity" d="'
        + _series_path(strategy, low, high, left, top, plot_width, plot_height)
        + '"/>'
    ]
    if reference_available:
        parts.append(
            '<path class="series reference-series" data-series="normalized-close-reference" d="'
            + _series_path(reference, low, high, left, top, plot_width, plot_height)
            + '"/><g class="legend"><text x="72" y="18">● '
            + _text(zh, "策略净值", "Strategy equity")
            + '</text><text x="210" y="18">┄ '
            + _text(zh, "收盘参考", "Close reference")
            + "</text></g>"
        )
    else:
        parts.append(
            '<text class="missing-state" x="460" y="155" text-anchor="middle">'
            + _text(zh, "参考序列不可用 / Reference series unavailable", "Reference series unavailable")
            + "</text>"
        )
    parts.append(_axis(rows, low, high, left, top, plot_width, plot_height, "×"))
    parts.append("</svg></section>")
    return "".join(parts)


def _drawdown_chart(rows, zh):
    title = _text(zh, "回撤路径", "Drawdown through time")
    if not rows:
        return '<section class="chart-card"><h2>' + title + '</h2><div class="chart-empty">' + _text(zh, "回撤序列不可用", "Drawdown series unavailable") + "</div></section>"
    equities = []
    for index in range(len(rows)):
        equities.append(float(rows[index].get("equity")))
    peak = equities[0]
    drawdowns = []
    for index in range(len(equities)):
        peak = max(peak, equities[index])
        drawdowns.append(equities[index] / peak - 1.0)
    low = min(drawdowns)
    high = 0.0
    if low == high:
        low = -0.01
    left = 62.0
    top = 28.0
    plot_width = 838.0
    plot_height = 242.0
    baseline = _y(0.0, low, high, top, plot_height)
    area = _series_path(drawdowns, low, high, left, top, plot_width, plot_height)
    area += " L" + _coord(left + plot_width) + " " + _coord(baseline)
    area += " L" + _coord(left) + " " + _coord(baseline) + " Z"
    parts = [
        '<section class="chart-card"><div class="chart-heading"><h2>'
        + title
        + "</h2><p>"
        + _text(zh, "基于已封存策略净值逐点投影；0% 为历史高点。", "Pointwise projection from sealed strategy equity; 0% is the running peak.")
        + '</p></div><svg class="chart chart-drawdown" role="img" aria-labelledby="drawdown-title drawdown-desc" viewBox="0 0 920 330" data-point-count="'
        + str(len(rows))
        + '"><title id="drawdown-title">'
        + title
        + '</title><desc id="drawdown-desc">'
        + _text(zh, "完整净值序列对应的逐日回撤。", "Drawdown for every canonical equity row.")
        + '</desc><path class="drawdown-area" d="'
        + area
        + '"/><path class="series drawdown-series" data-series="drawdown" d="'
        + _series_path(drawdowns, low, high, left, top, plot_width, plot_height)
        + '"/>'
        + _axis(rows, low * 100.0, high * 100.0, left, top, plot_width, plot_height, "%")
        + "</svg></section>"
    ]
    return "".join(parts)


def _trade_chart(trades, zh):
    title = _text(zh, "逐笔交易净损益", "Net P&L by trade")
    closed_count = 0
    open_count = 0
    values = []
    for index in range(len(trades)):
        values.append(float(trades[index].get("net_pnl_cny", 0.0)))
        if trades[index].get("status") == "CLOSED":
            closed_count += 1
        elif trades[index].get("status") == "OPEN":
            open_count += 1
    parts = [
        '<section class="chart-card"><div class="chart-heading"><h2>'
        + title
        + "</h2><p>"
        + _text(zh, "实心柱为已平仓净损益；虚线描边为未平仓盯市值，不虚构退出成本。", "Solid bars are closed-trade net P&L; dashed outlines are open mark-to-market values with no fabricated exit cost.")
        + '</p></div><svg class="chart chart-trade-pnl" role="img" aria-labelledby="trade-title trade-desc" viewBox="0 0 920 330" data-trade-count="'
        + str(len(trades))
        + '" data-closed-count="'
        + str(closed_count)
        + '" data-open-count="'
        + str(open_count)
        + '"><title id="trade-title">'
        + title
        + '</title><desc id="trade-desc">'
        + _text(zh, "每笔已平仓交易净损益柱，未平仓交易用独立虚线样式标记。", "One net-P&L bar per closed trade; open trades use a distinct dashed style.")
        + "</desc>"
    ]
    if not trades:
        parts.append('<text class="missing-state" x="460" y="155" text-anchor="middle">' + _text(zh, "没有交易 / No trades", "No trades") + "</text>")
    else:
        maximum = max([abs(value) for value in values] + [1.0])
        baseline = 160.0
        available_width = 820.0
        bar_slot = available_width / float(len(trades))
        bar_width = min(48.0, max(8.0, bar_slot * 0.58))
        parts.append('<line class="zero-line" x1="70" y1="160" x2="890" y2="160"/>')
        for index in range(len(trades)):
            trade = trades[index]
            value = values[index]
            magnitude = abs(value) / maximum * 112.0
            x_value = 70.0 + bar_slot * index + (bar_slot - bar_width) / 2.0
            y_value = baseline - magnitude if value >= 0 else baseline
            status = trade.get("status")
            if status == "OPEN":
                css_class = "open"
            elif value > 0:
                css_class = "closed positive"
            elif value < 0:
                css_class = "closed negative"
            else:
                css_class = "closed zero"
            parts.append(
                '<rect class="trade-bar '
                + css_class
                + '" data-trade-index="'
                + str(index)
                + '" data-status="'
                + _escape(status)
                + '" data-net-pnl-cny="'
                + _escape(value)
                + '" x="'
                + _coord(x_value)
                + '" y="'
                + _coord(y_value)
                + '" width="'
                + _coord(bar_width)
                + '" height="'
                + _coord(max(magnitude, 2.0))
                + '"><title>#'
                + str(index + 1)
                + " · "
                + _escape(status)
                + " · "
                + _escape(trade.get("entry_date"))
                + " · "
                + _money(value, zh)
                + "</title></rect>"
            )
            parts.append(
                '<text class="bar-label" x="'
                + _coord(x_value + bar_width / 2.0)
                + '" y="292" text-anchor="middle">#'
                + str(index + 1)
                + (" OPEN" if status == "OPEN" else "")
                + "</text>"
            )
    if closed_count == 0:
        parts.append('<text class="missing-state" x="460" y="42" text-anchor="middle">' + _text(zh, "没有已平仓交易 / No closed trades", "No closed trades") + "</text>")
    parts.append("</svg></section>")
    return "".join(parts)


def _table(rows, columns, labels, ledger, title, zh):
    parts = [
        '<section class="ledger-card"><h2>'
        + title
        + '</h2><div class="table-wrap"><table data-ledger="'
        + ledger
        + '" data-row-count="'
        + str(len(rows))
        + '"><thead><tr>'
    ]
    for column_index in range(len(columns)):
        column = columns[column_index]
        parts.append("<th scope=\"col\">" + _escape(labels.get(column, column.replace("_", " "))) + "</th>")
    parts.append("</tr></thead><tbody>")
    if not rows:
        parts.append('<tr><td colspan="' + str(len(columns)) + '" class="empty-cell">' + _text(zh, "无记录", "No rows") + "</td></tr>")
    for row_index in range(len(rows)):
        row = rows[row_index]
        parts.append('<tr data-row-index="' + str(row_index) + '">')
        for column_index in range(len(columns)):
            column = columns[column_index]
            value = row.get(column)
            parts.append("<td>" + _escape(value) + "</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div></section>")
    return "".join(parts)


def _metric_card(label, value, state):
    return '<article class="metric-card" data-state="' + state + '"><span>' + label + "</span><strong>" + _escape(value) + "</strong></article>"


def _period_metric(fields, zh):
    endpoints = (
        ("period_start", _text(zh, "报告起始", "Period start")),
        ("period_end", _text(zh, "报告结束", "Period end")),
    )
    values = []
    unavailable = []
    for field_id, label in endpoints:
        field = fields.get(field_id)
        if (
            type(field) is dict
            and field.get("availability") == "AVAILABLE"
            and field.get("raw") is not None
        ):
            values.append(field.get("raw"))
            continue
        reason = field.get("reason") if type(field) is dict else None
        if reason:
            label = label + (_text(zh, "：", ": ")) + str(reason)
        unavailable.append(label)
    if unavailable:
        return (
            _text(zh, "不可用：", "Unavailable: ")
            + _text(zh, "；", "; ").join(unavailable),
            "unavailable",
        )
    return str(values[0]) + " — " + str(values[1]), "available"


def _evidence(payload, zh):
    labels = {
        "identity_and_purpose": "身份与用途 / Identity and purpose",
        "evidence_status": "证据状态 / Evidence status",
        "account_summary": "账户摘要 / Account summary",
        "configuration": "配置 / Configuration",
        "price_equity_path": "价格与净值来源 / Price and equity source",
        "events_trades_holdings": "事件、交易与持仓来源 / Event, trade, and holding source",
        "costs_and_accounting": "成本与核算 / Costs and accounting",
        "total_return_claim": "总回报声明 / Total-return claim",
        "matched_exposure_qualification": "匹配敞口资格 / Matched-exposure qualification",
        "limitations": "限制 / Limitations",
        "provenance": "不可变来源 / Provenance",
    }
    parts = [
        '<details class="evidence-details"><summary>'
        + _text(zh, "证据、配置与不可变来源", "Evidence, configuration, and provenance")
        + '</summary><p class="detail-note">'
        + _text(zh, "以下保留 ReportDocument 的来源、可用性、限制与原始字段；完整行数据见上方台账。", "The ReportDocument sources, availability, limitations, and raw fields remain below; complete row data is in the ledgers above.")
        + "</p>"
    ]
    sections = payload["sections"]
    for section_index in range(len(sections)):
        section = sections[section_index]
        section_id = section["section_id"]
        parts.append('<details class="evidence-section"><summary>' + _escape(labels.get(section_id, section_id)) + '</summary><div class="table-wrap"><table><tbody>')
        fields = section["fields"]
        for field_index in range(len(fields)):
            field = fields[field_index]
            field_id = field["field_id"]
            shown = field.get("display")
            if shown is None:
                shown = field.get("raw") if field.get("availability") == "AVAILABLE" else field.get("reason")
            parts.append(
                "<tr><th scope=\"row\">"
                + _escape(field_id.replace("_", " "))
                + "</th><td><span>"
                + _escape(field.get("availability"))
                + "</span><br><code>"
                + _escape(shown)
                + "</code></td></tr>"
            )
        parts.append("</tbody></table></div><div class=\"source-list\">")
        for field_index in range(len(fields)):
            field = fields[field_index]
            source = field.get("source_ref") or {}
            parts.append(
                "<code>"
                + _escape(field["field_id"])
                + ": "
                + _escape(source.get("artifact"))
                + _escape(source.get("pointer"))
                + "</code>"
            )
        parts.append("</div></details>")
    parts.append("</details>")
    return "".join(parts)


def apply(payload, parameters):
    if parameters != {}:
        raise ValueError("canonical report parameters must be empty")
    fields = _field_map(payload)
    configuration = _raw(fields, "template_parameters")
    if type(configuration) is not dict:
        configuration = {}
    display_name = configuration.get("display_name")
    zh = display_name in {"黄金（Au99.99）", "交通银行"}
    if display_name is None:
        display_name = "Canonical Attempt"
    action = configuration.get("current_action")
    if type(action) is not dict:
        action = {}
    performance = configuration.get("performance_summary")
    if type(performance) is not dict:
        performance = {}
    action_name = action.get("action")
    action_available = action_name is not None
    if action_name is None:
        action_name = _text(zh, "不可用", "UNAVAILABLE")
    normalized_action = str(action_name).upper()
    action_css = (
        normalized_action.lower()
        if normalized_action in {"BUY", "SELL", "HOLD", "WAIT"}
        else "unavailable"
    )
    action_reason = action.get("reason")
    if action_reason is None:
        action_reason = _text(zh, "当前决策原因未在封存配置中提供。", "The sealed configuration does not provide a current decision reason.")
    as_of = action.get("latest_market_date")
    if as_of is None:
        as_of = _raw(fields, "period_end")
    current_position = _raw(fields, "current_position")
    qualification = configuration.get("qualification")
    if qualification is None:
        qualification = _text(zh, "资格状态不可用", "Qualification unavailable")
    rows = _raw(fields, "price_equity_rows") or []
    events = _raw(fields, "events") or []
    trades = _raw(fields, "trades") or []
    holdings = _raw(fields, "holdings") or []
    lang = "zh-CN" if zh else "en"
    title = _text(zh, "决策证据报告", "Decision evidence report")
    css = """*{box-sizing:border-box}body{margin:0;background:#f4f7f9;color:#18232d;font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}main{max-width:1180px;margin:auto;padding:22px}.decision-header{background:#102c3b;color:#fff;border-radius:18px;padding:26px;display:grid;grid-template-columns:1fr auto;gap:20px;align-items:center}.eyebrow{margin:0;color:#b7d7e7;font-weight:700}.decision-header h1{margin:4px 0 6px;font-size:clamp(1.7rem,4vw,2.8rem)}.as-of,.reason,.boundary,.qualification{margin:5px 0}.action-state{min-width:180px;text-align:center;border:3px solid #fff;border-radius:16px;padding:15px;font-size:2.1rem;font-weight:850;letter-spacing:.04em}.action-state.buy{background:#0c704b}.action-state.sell{background:#9b2c2c}.action-state.hold,.action-state.wait{background:#735a12}.action-state.unavailable{background:#4b5563}.metric-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}.metric-card{background:#fff;border:1px solid #d8e1e7;border-radius:13px;padding:14px;min-width:0}.metric-card span{display:block;color:#52616b;font-size:.82rem}.metric-card strong{display:block;margin-top:3px;font-size:1.12rem;overflow-wrap:anywhere}.metric-card[data-state=unavailable]{border-style:dashed}.chart-card,.ledger-card,.evidence-details{background:#fff;border:1px solid #d8e1e7;border-radius:15px;padding:18px;margin:14px 0}.chart-heading{display:flex;justify-content:space-between;gap:20px;align-items:baseline}.chart-heading h2,.ledger-card h2{margin:0}.chart-heading p{margin:0;color:#52616b;text-align:right}.chart{display:block;width:100%;height:auto;min-height:220px;margin-top:10px}.axis,.zero-line{stroke:#7b8790;stroke-width:1}.axis-label,.bar-label{font-size:11px;fill:#52616b}.series{fill:none;stroke-width:3;vector-effect:non-scaling-stroke}.price-series,.equity-series{stroke:#155e75}.reference-series{stroke:#9a6700;stroke-dasharray:9 6}.drawdown-series{stroke:#9b2c2c}.drawdown-area{fill:#f8d7da;opacity:.8}.holding-hatch{stroke:#628799;stroke-width:3}.holding-interval{opacity:.32}.event-marker text{font-size:11px;font-weight:800;paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round}.event-marker.buy polygon{fill:#0c704b;stroke:#083f2c;stroke-width:2}.event-marker.sell rect{fill:#9b2c2c;stroke:#5e1717;stroke-width:2}.trade-bar.closed.positive{fill:#0c704b}.trade-bar.closed.negative{fill:#9b2c2c}.trade-bar.closed.zero{fill:#66727a}.trade-bar.open{fill:#fff;stroke:#6d28d9;stroke-width:3;stroke-dasharray:7 4}.missing-state,.chart-empty{color:#5b6670;fill:#5b6670;font-weight:700}.table-wrap{overflow-x:auto;margin-top:10px}table{border-collapse:collapse;width:100%;font-size:.87rem}th,td{text-align:left;vertical-align:top;padding:9px 10px;border-bottom:1px solid #e2e8ec;white-space:nowrap}td:last-child,code{white-space:normal;overflow-wrap:anywhere}thead th{background:#edf3f6;position:sticky;top:0}.empty-cell{text-align:center;color:#66727a}.evidence-details>summary{font-size:1.15rem;font-weight:800;cursor:pointer}.evidence-section{border-top:1px solid #e2e8ec;padding:10px 0}.evidence-section summary{cursor:pointer;font-weight:700}.detail-note{color:#52616b}.boundary{font-weight:750;color:#f9d86b}@media(max-width:800px){.metric-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.chart-heading{display:block}.chart-heading p{text-align:left;margin-top:4px}.decision-header{grid-template-columns:1fr}.action-state{width:100%}}@media(max-width:640px){main{padding:10px}.decision-header{padding:18px;border-radius:12px}.metric-cards{grid-template-columns:1fr 1fr;gap:8px}.metric-card{padding:10px}.chart-card,.ledger-card,.evidence-details{padding:11px;border-radius:11px}.chart{min-width:620px}.chart-card{overflow-x:auto}.date-label{font-size:10px}th,td{padding:7px 8px;font-size:.8rem}}"""
    parts = [
        '<!doctype html><html lang="'
        + lang
        + '"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + _escape(display_name)
        + " · "
        + title
        + "</title><style>"
        + css
        + '</style></head><body><main><header class="decision-header"><div><p class="eyebrow">'
        + title
        + "</p><h1>"
        + _escape(display_name)
        + '</h1><p class="as-of">'
        + _text(zh, "数据截至：", "As of: ")
        + _escape(as_of)
        + " · "
        + _text(zh, "当前持仓：", "Current position: ")
        + _escape(current_position)
        + '</p><p class="reason">'
        + _escape(action_reason)
        + '</p><p class="qualification">'
        + _text(zh, "证据分类：", "Evidence classification: ")
        + _escape(qualification)
        + '</p><p class="boundary">'
        + _text(zh, "仅为研究证据，不自动下单 · research evidence / no automatic order", "research evidence / no automatic order")
        + '</p></div><div class="action-state '
        + action_css
        + ("" if action_available else " unavailable")
        + '">'
        + _escape(action_name)
        + "</div></header>"
    ]
    reference_return = performance.get("buy_and_hold_return")
    exposure = performance.get("exposure")
    period_value, period_state = _period_metric(fields, zh)
    parts.append('<section class="metric-cards" aria-label="' + _text(zh, "关键指标", "Key metrics") + '">')
    parts.append(_metric_card(_text(zh, "报告期间", "Period"), period_value, period_state))
    parts.append(_metric_card(_text(zh, "策略净回报", "Net return"), _percent(_raw(fields, "net_return"), zh), "available" if _raw(fields, "net_return") is not None else "unavailable"))
    parts.append(_metric_card(_text(zh, "同窗买入持有回报", "Same-window reference return"), _percent(reference_return, zh), "available" if reference_return is not None else "unavailable"))
    parts.append(_metric_card(_text(zh, "净损益", "Net P&L"), _money(_raw(fields, "net_profit_cny"), zh), "available" if _raw(fields, "net_profit_cny") is not None else "unavailable"))
    parts.append(_metric_card(_text(zh, "最大回撤", "Max drawdown"), _percent(_raw(fields, "max_drawdown"), zh), "available" if _raw(fields, "max_drawdown") is not None else "unavailable"))
    parts.append(_metric_card(_text(zh, "持仓占比", "Exposure"), _percent(exposure, zh), "available" if exposure is not None else "unavailable"))
    parts.append(_metric_card(_text(zh, "事件 / 交易", "Events / trades"), str(len(events)) + " / " + str(len(trades)), "available"))
    parts.append(_metric_card(_text(zh, "总成本", "Total costs"), _money(_raw(fields, "total_cost_cny"), zh), "available" if _raw(fields, "total_cost_cny") is not None else "unavailable"))
    parts.append("</section>")
    parts.append(_price_chart(rows, events, holdings, zh))
    parts.append(_equity_chart(rows, performance, zh))
    parts.append(_drawdown_chart(rows, zh))
    parts.append(_trade_chart(trades, zh))
    event_columns = ["Date", "side", "price", "quantity", "notional_cny", "commission_cny", "transfer_fee_cny", "stamp_tax_cny", "slippage_cny", "total_cost_cny", "cash_before_cny", "cash_after_cny", "holdings_before", "holdings_after", "reason"]
    event_labels = {"Date": "日期 / Date", "side": "动作 / Side", "price": "价格 / Price", "quantity": "数量 / Quantity", "reason": "原因 / Reason", "total_cost_cny": "总成本 / Total cost"}
    trade_columns = ["entry_date", "entry_price", "quantity", "entry_cost_cny", "exit_date", "exit_price", "exit_cost_cny", "status", "gross_pnl_cny", "net_pnl_cny", "return"]
    trade_labels = {"entry_date": "入场日 / Entry", "exit_date": "退出日 / Exit", "status": "状态 / Status", "net_pnl_cny": "净损益 / Net P&L", "return": "回报 / Return"}
    price_columns = ["date", "price", "close", "equity"]
    holding_columns = ["date", "holdings", "position_after"]
    parts.append(_table(events, event_columns, event_labels, "events", _text(zh, "完整 BUY / SELL 事件台账", "Complete BUY / SELL event ledger"), zh))
    parts.append(_table(trades, trade_columns, trade_labels, "trades", _text(zh, "完整交易台账", "Complete trade ledger"), zh))
    parts.append('<details class="series-ledgers"><summary>' + _text(zh, "展开完整价格、净值与每日持仓行", "Expand complete price, equity, and daily holding rows") + "</summary>")
    parts.append(_table(rows, price_columns, {}, "price-equity", _text(zh, "完整价格与净值行", "Complete price and equity rows"), zh))
    parts.append(_table(holdings, holding_columns, {}, "holdings", _text(zh, "完整每日持仓行", "Complete daily holding rows"), zh))
    parts.append("</details>")
    parts.append(_evidence(payload, zh))
    parts.append("</main></body></html>\n")
    return "".join(parts)
