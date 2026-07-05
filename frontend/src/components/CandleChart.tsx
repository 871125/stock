import { useEffect, useRef } from "react";
import {
  createChart,
  createSeriesMarkers,
  CandlestickSeries,
  LineSeries,
  LineStyle,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Candle, PivotPoint, Position } from "../types/backtest";

interface CandleChartProps {
  candles: Candle[];
  pivots?: PivotPoint[];
  positions?: Position[];
  height?: number;
}

function toUtcTimestamp(timestamp: string): UTCTimestamp {
  return Math.floor(new Date(timestamp).getTime() / 1000) as UTCTimestamp;
}

/**
 * lightweight-charts requires strictly increasing, unique times per series.
 * Two pivots can share a candle's timestamp (an outside bar confirms both an
 * SH and an SL at once) -- collapse those into the later pivot's value so the
 * connecting line has one point per timestamp.
 */
function dedupeByTime<T extends { time: UTCTimestamp }>(points: T[]): T[] {
  const result: T[] = [];
  for (const point of points) {
    if (result.length > 0 && result[result.length - 1].time === point.time) {
      result[result.length - 1] = point;
    } else {
      result.push(point);
    }
  }
  return result;
}

function readChartColors() {
  const style = getComputedStyle(document.documentElement);
  const read = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback;
  return {
    grid: read("--chart-grid", "#e1e0d9"),
    muted: read("--chart-muted", "#898781"),
    good: read("--chart-good", "#0ca30c"),
    critical: read("--chart-critical", "#d03b3b"),
    pivotSh: read("--chart-pivot-sh", "#2a78d6"),
    pivotSl: read("--chart-pivot-sl", "#1baf7a"),
  };
}

export function CandleChart({ candles, pivots = [], positions = [], height = 500 }: CandleChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current || candles.length === 0) return;

    const colors = readChartColors();

    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: colors.muted },
      grid: {
        vertLines: { color: colors.grid },
        horzLines: { color: colors.grid },
      },
      timeScale: { timeVisible: true, secondsVisible: false },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: colors.good,
      borderUpColor: colors.good,
      wickUpColor: colors.good,
      downColor: colors.critical,
      borderDownColor: colors.critical,
      wickDownColor: colors.critical,
    });
    candleSeries.setData(
      dedupeByTime(
        candles.map((c) => ({
          time: toUtcTimestamp(c.timestamp),
          open: c.open,
          high: c.high,
          low: c.low,
          close: c.close,
        })),
      ),
    );

    // Swing-point structure: a dotted zigzag connecting each confirmed SH/SL,
    // numbered in the order they were confirmed.
    if (pivots.length > 0) {
      const sortedPivots = [...pivots].sort((a, b) => a.sequence_no - b.sequence_no);

      const pivotLine = chart.addSeries(LineSeries, {
        color: colors.muted,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      pivotLine.setData(
        dedupeByTime(
          sortedPivots.map((p) => ({ time: toUtcTimestamp(p.timestamp), value: p.price })),
        ),
      );

      const pivotMarkers: SeriesMarker<Time>[] = sortedPivots.map((p) => ({
        time: toUtcTimestamp(p.timestamp),
        position: "atPriceMiddle",
        price: p.price,
        shape: "circle",
        color: p.type === "SH" ? colors.pivotSh : colors.pivotSl,
        text: `${p.type}${p.sequence_no}`,
        size: 0.6,
      }));
      createSeriesMarkers(pivotLine, pivotMarkers);
    }

    // Per-position EP arrow plus dashed SL/TP lines spanning the trade's lifetime,
    // all numbered by the position's sequence_no so they match the trade table below.
    for (const position of positions) {
      const isLong = position.side === "long";
      const sideColor = isLong ? colors.good : colors.critical;
      const entryT = toUtcTimestamp(position.entry_time);
      const exitT = toUtcTimestamp(
        position.exit_time ?? candles[candles.length - 1].timestamp,
      );

      createSeriesMarkers(candleSeries, [
        {
          time: entryT,
          position: isLong ? "belowBar" : "aboveBar",
          shape: isLong ? "arrowUp" : "arrowDown",
          color: sideColor,
          text: `#${position.sequence_no} EP`,
        },
      ]);

      const levelSpan = (value: number) =>
        exitT > entryT
          ? [
              { time: entryT, value },
              { time: exitT, value },
            ]
          : [{ time: entryT, value }];

      const slLine = chart.addSeries(LineSeries, {
        color: colors.critical,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      slLine.setData(levelSpan(position.stop_loss));
      createSeriesMarkers(slLine, [
        {
          time: entryT,
          position: "atPriceMiddle",
          price: position.stop_loss,
          shape: "square",
          color: colors.critical,
          text: `#${position.sequence_no} SL`,
          size: 0.5,
        },
      ]);

      const tpLine = chart.addSeries(LineSeries, {
        color: colors.good,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      tpLine.setData(levelSpan(position.take_profit));
      createSeriesMarkers(tpLine, [
        {
          time: entryT,
          position: "atPriceMiddle",
          price: position.take_profit,
          shape: "square",
          color: colors.good,
          text: `#${position.sequence_no} TP`,
          size: 0.5,
        },
      ]);
    }

    return () => chart.remove();
  }, [candles, pivots, positions]);

  return <div ref={containerRef} style={{ width: "100%", height }} />;
}
