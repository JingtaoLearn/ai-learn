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


def _chart_range(start, end, low, high, zh):
    return (
        '<div class="chart-range" aria-label="'
        + _text(zh, "图表范围", "Chart range")
        + '"><span>'
        + _escape(start)
        + " → "
        + _escape(end)
        + '</span><span class="numeric">'
        + _escape(low)
        + " — "
        + _escape(high)
        + "</span></div>"
    )


def _position_intervals(rows, holdings):
    intervals = []
    if not rows:
        return intervals
    if len(holdings) != len(rows):
        raise ValueError("holdings must align one-to-one with price rows by date")
    for index in range(len(rows)):
        if (
            holdings[index].get("date") != rows[index].get("date")
            or "position_after" not in holdings[index]
        ):
            raise ValueError("holdings must align one-to-one with price rows by date")
    state = None
    start = 0
    for index in range(len(rows)):
        position_after = holdings[index]["position_after"]
        next_state = "HOLDING" if int(position_after) != 0 else "CASH"
        if state is None:
            state = next_state
        elif next_state != state:
            intervals.append({"state": state, "start": start, "end": index - 1})
            state = next_state
            start = index
    intervals.append({"state": state, "start": start, "end": len(rows) - 1})
    return intervals


def _event_side(event):
    side = str(event.get("side", "")).strip().upper()
    return side if side in {"BUY", "SELL"} else "UNAVAILABLE"


def _state_timeline(rows, intervals, zh):
    focus_start = 0
    context = max(1, int(len(rows) * 0.025))
    transition_starts = [interval["start"] for interval in intervals[1:]]
    dense_start = None
    dense_span = max(2, int(len(rows) * 0.05))
    for index in range(len(transition_starts) - 2):
        if (
            transition_starts[index] > len(rows) * 0.35
            and transition_starts[index + 2] - transition_starts[index] <= dense_span
        ):
            dense_start = transition_starts[index]
            break
    if dense_start is not None:
        focus_start = max(0, dense_start - context)
    elif transition_starts and transition_starts[0] > len(rows) * 0.35:
        focus_start = max(0, transition_starts[0] - context)
    focus_end = len(rows) - 1
    focus_count = focus_end - focus_start + 1
    step = 100.0 / float(max(focus_count - 1, 1))
    parts = [
        '<div class="state-timeline" data-focus-start-index="'
        + str(focus_start)
        + '" data-focus-end-index="'
        + str(focus_end)
        + '" data-full-start-date="'
        + _escape(rows[0].get("date"))
        + '" data-full-end-date="'
        + _escape(rows[-1].get("date"))
        + '"><div class="state-timeline-heading"><strong>'
        + _text(zh, "持仓状态时间线", "Position-state timeline")
        + '</strong><span class="numeric">'
        + _escape(rows[focus_start].get("date"))
        + " → "
        + _escape(rows[focus_end].get("date"))
        + '</span></div><div class="state-track" role="img" aria-label="'
        + _text(zh, "展开的连续持仓与空仓状态时间线", "Expanded continuous HOLDING and CASH state timeline")
        + '">'
    ]
    for interval in intervals:
        if interval["end"] < focus_start:
            continue
        start = max(interval["start"], focus_start)
        end = min(interval["end"], focus_end)
        if focus_count == 1:
            start_percent = 0.0
            end_percent = 100.0
        else:
            start_percent = max(0.0, (start - focus_start) * step - step / 2.0)
            end_percent = min(100.0, (end - focus_start) * step + step / 2.0)
        width_percent = end_percent - start_percent
        state = interval["state"]
        state_class = "holding" if state == "HOLDING" else "cash"
        transition_class = ""
        if interval["start"] > focus_start:
            transition_class = " transition-buy" if state == "HOLDING" else " transition-sell"
        label = _text(zh, "持仓 / HOLDING", "HOLDING")
        if state == "CASH":
            label = _text(zh, "空仓 / CASH", "CASH")
        parts.append(
            '<span class="state-track-segment '
            + state_class
            + transition_class
            + '" data-state="'
            + state
            + '" data-start-index="'
            + str(start)
            + '" data-end-index="'
            + str(end)
            + '" aria-label="'
            + state
            + " · "
            + _escape(rows[start].get("date"))
            + " — "
            + _escape(rows[end].get("date"))
            + '" style="left:'
            + _coord(start_percent)
            + "%;width:"
            + _coord(width_percent)
            + '%">'
        )
        if width_percent >= 12.0:
            parts.append('<span class="state-track-label">' + label + "</span>")
        parts.append("</span>")
    parts.append('</div><div class="state-timeline-axis">')
    for index in (focus_start, focus_start + (focus_count - 1) // 2, focus_end):
        date_value = rows[index].get("date")
        parts.append('<time datetime="' + _escape(date_value) + '">' + _escape(date_value) + "</time>")
    parts.append("</div></div>")
    return "".join(parts)


def _price_chart(rows, events, holdings, zh):
    title = _text(zh, "价格与持仓状态", "Price and position state")
    if not rows:
        return (
            '<section class="chart-card" id="price-history"><h2>'
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
        side = _event_side(events[index])
        if side == "BUY":
            buy_count += 1
        elif side == "SELL":
            sell_count += 1
    intervals = _position_intervals(rows, holdings)
    transitions = []
    for index in range(1, len(intervals)):
        previous = intervals[index - 1]
        current = intervals[index]
        side = "BUY" if current["state"] == "HOLDING" else "SELL"
        transitions.append(
            {
                "side": side,
                "date": rows[current["start"]].get("date"),
                "from_state": previous["state"],
                "to_state": current["state"],
                "index": current["start"],
            }
        )
    parts = [
        '<section class="chart-card" id="price-history"><div class="chart-heading"><h2>'
        + title
        + '</h2><div class="state-legend" aria-label="'
        + _text(zh, "持仓状态图例", "Position-state legend")
        + '"><span class="state-legend-item holding"><i aria-hidden="true"></i>持仓 / HOLDING</span>'
        + '<span class="state-legend-item cash"><i aria-hidden="true"></i>空仓 / CASH</span>'
        + '<span class="boundary-legend"><i class="buy" aria-hidden="true"></i>买入 BUY：CASH→HOLDING · '
        + '<i class="sell" aria-hidden="true"></i>卖出 SELL：HOLDING→CASH</span></div></div>'
        + _chart_range(
            rows[0].get("date"),
            rows[-1].get("date"),
            _number(low),
            _number(high),
            zh,
        )
        + '<svg class="chart chart-price" role="img" aria-labelledby="price-title price-desc" '
        + 'viewBox="0 0 920 330" data-point-count="'
        + str(len(rows))
        + '" data-event-count="'
        + str(len(events))
        + '" data-buy-count="'
        + str(buy_count)
        + '" data-sell-count="'
        + str(sell_count)
        + '" data-position-interval-count="'
        + str(len(intervals))
        + '" data-transition-count="'
        + str(len(transitions))
        + '"><title id="price-title">'
        + title
        + '</title><desc id="price-desc">'
        + _text(zh, "完整价格路径与连续持仓/空仓区间。实线边界表示空仓转持仓，虚线边界表示持仓转空仓；精确事件明细保留在下方台账。", "Complete price path with continuous HOLDING/CASH intervals. Solid boundaries mean CASH to HOLDING and dashed boundaries mean HOLDING to CASH; exact event details remain in the ledger below.")
        + '</desc><defs><pattern id="price-holding-pattern" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="8" height="8" fill="#e8f3ee"/><line x1="0" y1="0" x2="0" y2="8" stroke="#78988a" stroke-width="2"/></pattern><pattern id="price-cash-pattern" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" fill="#f4f5f7"/><circle cx="2" cy="2" r="1.2" fill="#8b949e"/></pattern></defs>',
    ]
    step = plot_width / float(max(len(rows) - 1, 1))
    for interval_index in range(len(intervals)):
        interval = intervals[interval_index]
        state = interval["state"]
        start = interval["start"]
        end = interval["end"]
        start_x = max(left, _x(start, len(rows), left, plot_width) - step / 2.0)
        end_x = min(left + plot_width, _x(end, len(rows), left, plot_width) + step / 2.0)
        state_class = "holding" if state == "HOLDING" else "cash"
        parts.append(
            '<rect class="position-interval '
            + state_class
            + '-interval" data-state="'
            + state
            + '" data-start-index="'
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
            + _coord(end_x - start_x)
            + '" height="'
            + _coord(plot_height)
            + '" fill="url(#price-'
            + state_class
            + '-pattern)"><title>'
            + state
            + " · "
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
    for transition in transitions:
        boundary_x = max(
            left,
            _x(transition["index"], len(rows), left, plot_width) - step / 2.0,
        )
        parts.append(
            '<line class="transition-boundary '
            + transition["side"].lower()
            + '" data-side="'
            + transition["side"]
            + '" data-date="'
            + _escape(transition["date"])
            + '" data-from-state="'
            + transition["from_state"]
            + '" data-to-state="'
            + transition["to_state"]
            + '" x1="'
            + _coord(boundary_x)
            + '" y1="'
            + _coord(top)
            + '" x2="'
            + _coord(boundary_x)
            + '" y2="'
            + _coord(top + plot_height)
            + '"><title>'
            + transition["side"]
            + " · "
            + _escape(transition["date"])
            + " · "
            + transition["from_state"]
            + " → "
            + transition["to_state"]
            + "</title></line>"
        )
    parts.append(_axis(rows, low, high, left, top, plot_width, plot_height, ""))
    parts.append("</svg>" + _state_timeline(rows, intervals, zh) + "</section>")
    return "".join(parts)


def _equity_chart(rows, performance, zh):
    title = _text(zh, "策略净值与可用参考", "Strategy equity and available reference")
    if not rows:
        return '<section class="chart-card" id="equity-history"><h2>' + title + '</h2><div class="chart-empty">' + _text(zh, "净值序列不可用", "Equity series unavailable") + "</div></section>"
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
        '<section class="chart-card" id="equity-history"><div class="chart-heading"><h2>'
        + title
        + "</h2><p>"
        + _text(zh, "策略净值以首日为 1.00；参考仅在来源字段合格可用时显示。", "Strategy equity starts at 1.00; a reference is shown only when its source field is available.")
        + "</p></div>"
        + _chart_range(
            rows[0].get("date"),
            rows[-1].get("date"),
            "%.2f×" % low,
            "%.2f×" % high,
            zh,
        )
        + '<svg class="chart chart-equity" role="img" aria-labelledby="equity-title equity-desc" viewBox="0 0 920 330" data-point-count="'
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
        return '<section class="chart-card" id="drawdown-history"><h2>' + title + '</h2><div class="chart-empty">' + _text(zh, "回撤序列不可用", "Drawdown series unavailable") + "</div></section>"
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
        '<section class="chart-card" id="drawdown-history"><div class="chart-heading"><h2>'
        + title
        + "</h2><p>"
        + _text(zh, "基于已封存策略净值逐点投影；0% 为历史高点。", "Pointwise projection from sealed strategy equity; 0% is the running peak.")
        + "</p></div>"
        + _chart_range(
            rows[0].get("date"),
            rows[-1].get("date"),
            "%.2f%%" % (low * 100.0),
            "0.00%",
            zh,
        )
        + '<svg class="chart chart-drawdown" role="img" aria-labelledby="drawdown-title drawdown-desc" viewBox="0 0 920 330" data-point-count="'
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
        '<section class="chart-card" id="trade-history"><div class="chart-heading"><h2>'
        + title
        + "</h2><p>"
        + _text(zh, "实心柱为已平仓净损益；虚线描边为未平仓盯市值，不虚构退出成本。", "Solid bars are closed-trade net P&L; dashed outlines are open mark-to-market values with no fabricated exit cost.")
        + "</p></div>"
        + _chart_range(
            "#1" if trades else _text(zh, "无交易", "No trades"),
            "#" + str(len(trades)) if trades else _text(zh, "无交易", "No trades"),
            _money(min(values), zh) if values else _money(0.0, zh),
            _money(max(values), zh) if values else _money(0.0, zh),
            zh,
        )
        + '<svg class="chart chart-trade-pnl" role="img" aria-labelledby="trade-title trade-desc" viewBox="0 0 920 330" data-trade-count="'
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


def _ledger_detail(row, columns, zh, accessible_name):
    parts = [
        '<details class="ledger-details"><summary aria-label="'
        + _escape(accessible_name)
        + '">'
        + _text(zh, "完整核算", "All fields")
        + '</summary><dl>'
    ]
    for column in columns:
        parts.append(
            "<div><dt>"
            + _escape(column.replace("_", " "))
            + "</dt><dd>"
            + _escape(row.get(column))
            + "</dd></div>"
        )
    parts.append("</dl></details>")
    return "".join(parts)


def _event_ledger(rows, columns, title, zh):
    parts = [
        '<section class="ledger-card" id="event-ledger"><div class="section-heading"><div><p class="section-kicker">'
        + _text(zh, "决策轨迹", "Decision trail")
        + "</p><h2>"
        + title
        + "</h2></div><span>"
        + str(len(rows))
        + _text(zh, " 条事件", " events")
        + '</span></div><div class="table-wrap"><table class="compact-ledger" data-ledger="events" data-row-count="'
        + str(len(rows))
        + '"><thead><tr><th scope="col">'
        + _text(zh, "日期 / 动作 / 成交 / 仓位", "Date / action / execution / position")
        + '</th><th scope="col">'
        + _text(zh, "原因", "Reason")
        + '</th><th scope="col">'
        + _text(zh, "核算明细", "Accounting detail")
        + "</th></tr></thead><tbody>"
    ]
    if not rows:
        parts.append('<tr><td colspan="3" class="empty-cell">' + _text(zh, "无记录", "No rows") + "</td></tr>")
    for index in range(len(rows)):
        row = rows[index]
        side = str(row.get("side", "UNAVAILABLE")).upper()
        symbol = "▲" if side == "BUY" else "■" if side == "SELL" else "•"
        side_class = side.lower() if side in {"BUY", "SELL"} else "unavailable"
        transition = str(row.get("holdings_before", "—")) + " → " + str(
            row.get("holdings_after", "—")
        )
        parts.append(
            '<tr class="ledger-row event-row" data-row-index="'
            + str(index)
            + '"><td class="ledger-primary"><span data-primary-field="date">'
            + _escape(row.get("Date"))
            + '</span><span data-primary-field="action"><span class="status-label '
            + side_class
            + '">'
            + _escape(side)
            + " "
            + symbol
            + '</span></span><span data-primary-field="price">'
            + _number(row.get("price"))
            + '</span><span data-primary-field="transition">'
            + _escape(transition)
            + '</span></td><td class="ledger-reason">'
            + _escape(row.get("reason"))
            + '</td><td class="ledger-disclosure">'
            + _ledger_detail(
                row,
                columns,
                zh,
                _text(zh, "事件 ", "Event ")
                + str(index + 1)
                + " · "
                + str(row.get("Date", "—"))
                + " · "
                + side,
            )
            + "</td></tr>"
        )
    parts.append("</tbody></table></div></section>")
    return "".join(parts)


def _trade_ledger(rows, columns, title, zh):
    parts = [
        '<section class="ledger-card" id="trade-ledger"><div class="section-heading"><div><p class="section-kicker">'
        + _text(zh, "交易结果", "Trade outcomes")
        + "</p><h2>"
        + title
        + "</h2></div><span>"
        + str(len(rows))
        + _text(zh, " 笔交易", " trades")
        + '</span></div><div class="table-wrap"><table class="compact-ledger" data-ledger="trades" data-row-count="'
        + str(len(rows))
        + '"><thead><tr><th scope="col">'
        + _text(zh, "期间 / 状态 / 数量 / 净损益", "Period / status / quantity / net P&L")
        + '</th><th scope="col">'
        + _text(zh, "回报", "Return")
        + '</th><th scope="col">'
        + _text(zh, "核算明细", "Accounting detail")
        + "</th></tr></thead><tbody>"
    ]
    if not rows:
        parts.append('<tr><td colspan="3" class="empty-cell">' + _text(zh, "无记录", "No rows") + "</td></tr>")
    for index in range(len(rows)):
        row = rows[index]
        status = str(row.get("status", "UNAVAILABLE")).upper()
        status_class = status.lower() if status in {"OPEN", "CLOSED"} else "unavailable"
        period = str(row.get("entry_date", "—")) + " → " + str(
            row.get("exit_date") or _text(zh, "未平仓", "Open")
        )
        parts.append(
            '<tr class="ledger-row trade-row" data-row-index="'
            + str(index)
            + '"><td class="ledger-primary"><span data-primary-field="date">'
            + _escape(period)
            + '</span><span data-primary-field="action"><span class="status-label '
            + status_class
            + '">'
            + _escape(status)
            + '</span></span><span data-primary-field="quantity">×'
            + _escape(row.get("quantity"))
            + '</span><span data-primary-field="pnl">'
            + _money(row.get("net_pnl_cny"), zh)
            + '</span></td><td class="ledger-reason">'
            + _percent(row.get("return"), zh)
            + '</td><td class="ledger-disclosure">'
            + _ledger_detail(
                row,
                columns,
                zh,
                _text(zh, "交易 ", "Trade ")
                + str(index + 1)
                + " · "
                + str(row.get("entry_date", "—"))
                + " · "
                + status,
            )
            + "</td></tr>"
        )
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
        '<details class="evidence-details" id="evidence"><summary>'
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


def _stylesheet():
    return """:root{--canvas:#f3f5f7;--surface:#fff;--surface-alt:#f7f8fa;--ink:#151a20;--muted:#58636f;--line:#d9dee5;--line-strong:#b8c0ca;--accent:#1559d6;--buy:#087a52;--buy-bg:#e8f6f0;--sell:#b42318;--sell-bg:#fff0ee;--hold:#3f4852;--hold-bg:#eef1f4;--focus:#005fcc}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--canvas);color:var(--ink);font:14px/1.45 Arial,"Helvetica Neue",sans-serif;font-variant-numeric:tabular-nums}main{max-width:1180px;margin:auto;padding:18px}.decision-header{background:var(--surface);border:1px solid var(--line-strong);border-radius:10px;padding:18px 20px;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px;align-items:center}.eyebrow,.section-kicker{margin:0;color:var(--accent);font-size:.72rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.decision-header h1{margin:2px 0 5px;font-size:clamp(1.4rem,2.5vw,2rem);line-height:1.15;letter-spacing:-.02em}.as-of,.reason,.boundary,.qualification{margin:3px 0}.as-of{font-weight:700}.reason{font-size:1rem}.qualification{color:var(--muted);font-size:.82rem}.boundary{color:#6f2f00;font-weight:750}.action-state{min-width:148px;text-align:center;border:1px solid currentColor;border-radius:8px;padding:12px 16px;font-size:1.7rem;font-weight:850;letter-spacing:.04em}.action-state.buy{background:var(--buy-bg);color:#05633f}.action-state.sell{background:var(--sell-bg);color:#9c1c13}.action-state.hold,.action-state.wait{background:var(--hold-bg);color:var(--hold)}.action-state.unavailable{background:#f1f2f4;color:#414b55}.report-nav{display:flex;gap:4px;margin:10px 0 14px;padding:4px;background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow-x:auto;scrollbar-width:thin}.report-nav a{min-height:36px;padding:8px 11px;display:inline-flex;align-items:center;color:#33404c;text-decoration:none;white-space:nowrap;border-radius:5px;font-size:.8rem;font-weight:700}.report-nav a:hover{background:#edf3ff;color:#104bb4}.metric-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:0 0 12px}.metric-card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:11px 12px;min-width:0}.metric-card span{display:block;color:var(--muted);font-size:.75rem;font-weight:700}.metric-card strong{display:block;margin-top:2px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:1rem;line-height:1.3;overflow-wrap:anywhere}.metric-card[data-state=unavailable]{border-style:dashed}.chart-card,.ledger-card,.evidence-details{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:15px 16px;margin:10px 0}.chart-heading,.section-heading{display:flex;justify-content:space-between;gap:18px;align-items:baseline}.chart-heading h2,.section-heading h2,.ledger-card h2{margin:0;font-size:1.05rem;line-height:1.3}.chart-heading p{max-width:58%;margin:0;color:var(--muted);font-size:.78rem;text-align:right}.section-heading>span{color:var(--muted);font-size:.78rem;font-weight:700}.chart-range{display:flex;justify-content:space-between;gap:12px;margin:8px 0 -2px;color:var(--muted);font-size:.75rem;font-weight:700}.numeric{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.chart{display:block;width:100%;height:300px;margin-top:4px}.axis,.zero-line{stroke:#7a8490;stroke-width:1}.axis-label,.bar-label{font-size:12px;fill:#4d5965}.series{fill:none;stroke-width:2.5;vector-effect:non-scaling-stroke}.price-series,.equity-series{stroke:var(--accent)}.reference-series{stroke:#8a5a00;stroke-dasharray:9 6}.drawdown-series{stroke:var(--sell)}.drawdown-area{fill:#f5cbc6;opacity:.7}.trade-bar.closed.positive{fill:var(--buy)}.trade-bar.closed.negative{fill:var(--sell)}.trade-bar.closed.zero{fill:#66727a}.trade-bar.open{fill:#fff;stroke:#604ca6;stroke-width:3;stroke-dasharray:7 4}.missing-state,.chart-empty{color:var(--muted);fill:var(--muted);font-weight:700}.table-wrap{max-width:100%;overflow-x:auto;margin-top:8px}table{border-collapse:collapse;width:100%;font-size:.82rem}th,td{text-align:left;vertical-align:top;padding:8px 9px;border-bottom:1px solid #e5e8ec;white-space:nowrap}td:last-child,code{white-space:normal;overflow-wrap:anywhere}thead th{background:var(--surface-alt);color:#46515d;font-size:.72rem;letter-spacing:.02em}.empty-cell{text-align:center;color:var(--muted)}.compact-ledger{table-layout:fixed}.compact-ledger th:first-child{width:51%}.compact-ledger th:nth-child(2){width:31%}.compact-ledger th:last-child{width:18%}.ledger-row>td{height:64px;vertical-align:middle}.ledger-primary{display:grid;grid-template-columns:1.35fr .72fr .72fr 1fr;gap:8px;align-items:center;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.ledger-primary>span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.status-label{display:inline-flex;align-items:center;min-height:24px;padding:2px 7px;border-radius:4px;font-family:Arial,"Helvetica Neue",sans-serif;font-size:.72rem;font-weight:850}.status-label.buy{background:var(--buy-bg);color:#05633f}.status-label.sell{background:var(--sell-bg);color:#9c1c13}.status-label.open{background:#f2efff;color:#4d358f;border:1px dashed #7561b6}.status-label.closed{background:var(--hold-bg);color:var(--hold)}.status-label.unavailable{background:#f1f2f4;color:#414b55}.ledger-reason{line-height:1.35;white-space:normal;overflow-wrap:normal;word-break:normal}.ledger-details summary,.series-ledgers>summary,.evidence-details>summary,.evidence-section summary{cursor:pointer}.ledger-details summary{min-height:44px;display:inline-flex;align-items:center;color:var(--accent);font-size:.78rem;font-weight:800}.ledger-details dl{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:8px 0 4px}.ledger-details dl div{min-width:0;padding:7px;background:var(--surface-alt);border-radius:5px}.ledger-details dt{color:var(--muted);font-size:.68rem;text-transform:capitalize}.ledger-details dd{margin:2px 0 0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;overflow-wrap:anywhere}.ledger-details[open]{min-width:510px}.series-ledgers{margin:10px 0;padding:0 2px}.series-ledgers>summary{min-height:44px;display:flex;align-items:center;font-weight:800}.evidence-details>summary{min-height:44px;display:flex;align-items:center;font-size:1rem;font-weight:800}.evidence-section{border-top:1px solid #e2e8ec;padding:5px 0}.evidence-section summary{min-height:44px;display:flex;align-items:center;font-weight:700}.detail-note{color:var(--muted)}.source-list{display:grid;gap:4px}.source-list code{font-size:.75rem}:focus-visible{outline:3px solid var(--focus);outline-offset:3px;border-radius:3px}@media(max-width:800px){main{padding:12px}.metric-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.chart-heading{display:block}.chart-heading p{max-width:none;text-align:left;margin-top:3px}.chart{height:260px}}@media(max-width:640px){body{font-size:13px}main{padding:8px}.decision-header{padding:12px;grid-template-columns:minmax(0,1fr) 90px;gap:10px;border-radius:7px}.decision-header h1{font-size:1.3rem}.eyebrow{font-size:.65rem}.as-of,.reason,.boundary,.qualification{margin:2px 0}.reason{font-size:.88rem;line-height:1.32}.qualification{font-size:.72rem}.boundary{font-size:.74rem}.action-state{min-width:0;padding:10px 5px;font-size:1.15rem}.report-nav{margin:7px 0 9px}.report-nav a{min-height:44px;padding:9px}.metric-cards{gap:6px;margin-bottom:8px}.metric-card{padding:8px}.metric-card strong{font-size:.86rem}.chart-card,.ledger-card,.evidence-details{padding:10px;margin:7px 0;border-radius:7px}.chart-heading h2,.section-heading h2,.ledger-card h2{font-size:.95rem}.chart-heading p{font-size:.72rem}.chart-range{font-size:.7rem}.chart{height:220px;min-height:0}.axis-label,.bar-label{display:none}.compact-ledger,.compact-ledger tbody{display:block}.compact-ledger thead{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.compact-ledger .ledger-row{display:grid;grid-template-columns:minmax(0,1fr) 96px;gap:3px 8px;padding:7px 0;min-height:88px;border-bottom:1px solid #e5e8ec}.compact-ledger .ledger-row>td{height:auto;padding:0;border:0}.ledger-primary{grid-column:1/-1;grid-template-columns:1.35fr .72fr .65fr .95fr;gap:5px;font-size:.75rem}.ledger-reason{grid-column:1;display:-webkit-box;min-width:0;overflow:hidden;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.35}.ledger-disclosure{grid-column:2;grid-row:2}.ledger-details summary{width:100%;min-height:44px;justify-content:flex-end}.ledger-details[open]{min-width:0}.ledger-details[open] dl{position:relative;z-index:1;grid-template-columns:repeat(2,minmax(0,1fr));width:calc(100vw - 38px);margin-left:calc(-100vw + 126px)}.series-ledgers>summary,.evidence-details>summary,.evidence-section summary{min-height:44px}th,td{padding:7px 8px;font-size:.76rem}.date-label{display:none}}@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}"""


def _ledger_styles():
    return """.compact-ledger .ledger-row>td{height:auto;padding:5px 8px}.ledger-reason{display:-webkit-box;max-height:2.7em;overflow:hidden;-webkit-line-clamp:2;-webkit-box-orient:vertical}.state-legend{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px 12px;color:var(--muted);font-size:.75rem}.state-legend-item,.boundary-legend{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}.state-legend-item i{width:18px;height:12px;border:1px solid var(--line-strong)}.state-legend-item.holding i{background:repeating-linear-gradient(135deg,#e8f3ee 0,#e8f3ee 4px,#78988a 4px,#78988a 6px)}.state-legend-item.cash i{background-color:#f4f5f7;background-image:radial-gradient(#8b949e 1px,transparent 1px);background-size:5px 5px}.boundary-legend i{display:inline-block;width:2px;height:14px;border-left:2px solid var(--ink)}.boundary-legend i.sell{border-left-style:dashed}.position-interval{opacity:.55}.position-interval.holding-interval{stroke:#78988a;stroke-width:.5}.position-interval.cash-interval{stroke:#8b949e;stroke-width:.5}.state-timeline{margin-top:6px}.state-timeline-heading{display:flex;justify-content:space-between;gap:12px;margin-bottom:4px;color:var(--muted);font-size:.72rem}.state-timeline-heading strong{color:var(--ink)}.state-track{position:relative;height:38px;overflow:hidden;border:1px solid var(--line-strong);background:var(--surface-alt)}.state-track-segment{position:absolute;top:0;height:100%;min-width:0;overflow:hidden;display:flex;align-items:center;justify-content:center;box-shadow:inset -1px 0 #fff}.state-track-segment.holding{background:repeating-linear-gradient(135deg,#e8f3ee 0,#e8f3ee 5px,#78988a 5px,#78988a 7px)}.state-track-segment.cash{background-color:#f4f5f7;background-image:radial-gradient(#8b949e 1px,transparent 1px);background-size:6px 6px}.state-track-segment.transition-buy::before,.state-track-segment.transition-sell::before{content:"";position:absolute;inset:0 auto 0 0;width:0;border-left:2px solid var(--ink)}.state-track-segment.transition-sell::before{border-left-style:dashed}.state-track-label{padding:2px 4px;background:rgba(255,255,255,.82);color:var(--ink);font-size:.7rem;font-weight:850;white-space:nowrap}.state-timeline-axis{display:grid;grid-template-columns:repeat(3,1fr);margin-top:3px;color:var(--muted);font-size:.68rem}.state-timeline-axis time:nth-child(2){text-align:center}.state-timeline-axis time:last-child{text-align:right}.transition-boundary{stroke:var(--ink);stroke-width:2;vector-effect:non-scaling-stroke}.transition-boundary.buy{stroke-dasharray:none}.transition-boundary.sell{stroke-dasharray:6 4}@media(max-width:640px){.chart-heading .state-legend{justify-content:flex-start}.state-legend{gap:5px 9px;font-size:.72rem}.state-track-label{display:none}.chart-price .date-label{display:block;font-size:24px}}"""


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
    css = _stylesheet() + _ledger_styles()
    parts = [
        '<!doctype html><html lang="'
        + lang
        + '"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + _escape(display_name)
        + " · "
        + title
        + "</title><style>"
        + css
        + '</style></head><body><main><header class="decision-header" id="decision"><div><p class="eyebrow">'
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
        + '</div></header><nav class="report-nav" aria-label="'
        + _text(zh, "报告导航", "Report navigation")
        + '"><a href="#decision">'
        + _text(zh, "当前决策", "Decision")
        + '</a><a href="#summary">'
        + _text(zh, "摘要", "Summary")
        + '</a><a href="#price-history">'
        + _text(zh, "价格", "Price")
        + '</a><a href="#drawdown-history">'
        + _text(zh, "风险", "Risk")
        + '</a><a href="#event-ledger">'
        + _text(zh, "台账", "Ledgers")
        + '</a><a href="#evidence">'
        + _text(zh, "证据", "Evidence")
        + "</a></nav>"
    ]
    reference_return = performance.get("buy_and_hold_return")
    exposure = performance.get("exposure")
    period_value, period_state = _period_metric(fields, zh)
    parts.append('<section class="metric-cards" id="summary" aria-label="' + _text(zh, "关键指标", "Key metrics") + '">')
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
    trade_columns = ["entry_date", "entry_price", "quantity", "entry_cost_cny", "exit_date", "exit_price", "exit_cost_cny", "status", "gross_pnl_cny", "net_pnl_cny", "return"]
    price_columns = ["date", "price", "close", "equity"]
    holding_columns = ["date", "holdings", "position_after"]
    parts.append(_event_ledger(events, event_columns, _text(zh, "BUY / SELL 事件台账", "BUY / SELL event ledger"), zh))
    parts.append(_trade_ledger(trades, trade_columns, _text(zh, "交易台账", "Trade ledger"), zh))
    parts.append('<details class="series-ledgers" id="raw-data"><summary>' + _text(zh, "展开完整价格、净值与每日持仓行", "Expand complete price, equity, and daily holding rows") + "</summary>")
    parts.append(_table(rows, price_columns, {}, "price-equity", _text(zh, "完整价格与净值行", "Complete price and equity rows"), zh))
    parts.append(_table(holdings, holding_columns, {}, "holdings", _text(zh, "完整每日持仓行", "Complete daily holding rows"), zh))
    parts.append("</details>")
    parts.append(_evidence(payload, zh))
    parts.append("</main></body></html>\n")
    return "".join(parts)
