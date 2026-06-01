"use client";

import { useEffect, useRef } from "react";
import {
  createChart, ColorType, LineStyle, CrosshairMode,
  type IChartApi, type ISeriesApi, type IPriceLine,
} from "lightweight-charts";
import type { Candle } from "@/lib/api";

export interface TradeMarker {
  /** ISO-Time des Trade-Events */
  time: string;
  side: "long" | "short";
  /** 'open' = Entry-Marker, 'close' = Exit-Marker */
  kind: "open" | "close";
  price: number;
  label?: string;
}

interface Props {
  candles: Candle[];
  height?: number;
  /** Horizontale Linien (Entry/SL/TP), bleiben auf der gesamten Zeitachse */
  priceLines?: Array<{ price: number; color: string; label: string; dashed?: boolean }>;
  /** Punkt-Marker (Open/Close eines Trades), an konkretem Zeitpunkt */
  markers?: TradeMarker[];
}

/** Lightweight-Charts Candlestick + Linien für Entry/SL/TP. SSR-disabled. */
export default function TradeChart({ candles, height = 280, priceLines = [], markers = [] }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const priceLineRefs = useRef<IPriceLine[]>([]);

  // Chart einmalig erzeugen
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#1a1d27" },
        textColor: "#94a3b8",
      },
      grid: {
        vertLines: { color: "#2d3148" },
        horzLines: { color: "#2d3148" },
      },
      width: containerRef.current.clientWidth,
      height,
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#2d3148" },
      rightPriceScale: { borderColor: "#2d3148" },
      crosshair: { mode: CrosshairMode.Normal },
    });

    const series = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderUpColor: "#22c55e",
      borderDownColor: "#ef4444",
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const handleResize = () => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      priceLineRefs.current = [];
    };
  }, [height]);

  // Daten + Linien + Marker bei Prop-Change neu setzen
  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart || candles.length === 0) return;

    const data = candles.map((c) => ({
      time: (Math.floor(new Date(c.time).getTime() / 1000)) as unknown as string,
      open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    series.setData(data as Parameters<typeof series.setData>[0]);

    // Alte PriceLines entfernen
    for (const pl of priceLineRefs.current) {
      try { series.removePriceLine(pl); } catch {}
    }
    priceLineRefs.current = [];

    // Neue PriceLines anbringen (Entry/SL/TP)
    for (const ln of priceLines) {
      const pl = series.createPriceLine({
        price: ln.price,
        color: ln.color,
        lineWidth: 2,
        lineStyle: ln.dashed ? LineStyle.Dashed : LineStyle.Solid,
        axisLabelVisible: true,
        title: ln.label,
      });
      priceLineRefs.current.push(pl);
    }

    // Punkt-Marker (Entry-Pfeil, Exit-Punkt)
    if (markers.length > 0) {
      const m = markers.map((mk) => ({
        time: (Math.floor(new Date(mk.time).getTime() / 1000)) as unknown as string,
        position: mk.kind === "open"
          ? (mk.side === "long" ? "belowBar" as const : "aboveBar" as const)
          : "inBar" as const,
        color: mk.kind === "open"
          ? (mk.side === "long" ? "#22c55e" : "#ef4444")
          : "#94a3b8",
        shape: mk.kind === "open"
          ? (mk.side === "long" ? "arrowUp" as const : "arrowDown" as const)
          : "circle" as const,
        text: mk.label,
      }));
      series.setMarkers(m as Parameters<typeof series.setMarkers>[0]);
    } else {
      series.setMarkers([]);
    }

    chart.timeScale().fitContent();
  }, [candles, priceLines, markers]);

  return <div ref={containerRef} style={{ width: "100%", height }} />;
}
