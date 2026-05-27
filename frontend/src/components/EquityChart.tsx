"use client";

import { useEffect, useRef } from "react";
import { createChart, ColorType, LineStyle } from "lightweight-charts";

interface Point {
  time: string;
  value: number;
}

interface Props {
  data: Point[];
  initialCapital: number;
  height?: number;
}

export default function EquityChart({ data, initialCapital, height = 260 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current || data.length === 0) return;

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
      timeScale: { timeVisible: true, secondsVisible: false },
    });

    const series = chart.addLineSeries({
      color: "#3b82f6",
      lineWidth: 2,
      priceLineVisible: false,
    });

    // Baseline (initial capital)
    const baseline = chart.addLineSeries({
      color: "#4b5563",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      priceLineVisible: false,
    });

    // lightweight-charts braucht UNIX-Timestamp als number (Sekunden)
    const chartData = data.map((p) => ({
      time: Math.floor(new Date(p.time).getTime() / 1000) as unknown as string,
      value: p.value,
    }));
    series.setData(chartData as Parameters<typeof series.setData>[0]);

    const baselineData = [
      { time: chartData[0].time, value: initialCapital },
      { time: chartData[chartData.length - 1].time, value: initialCapital },
    ];
    baseline.setData(baselineData as Parameters<typeof baseline.setData>[0]);

    chart.timeScale().fitContent();

    const handleResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, [data, initialCapital, height]);

  return <div ref={containerRef} style={{ width: "100%", height }} />;
}
