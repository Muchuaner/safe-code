'use strict';

function createGatewayLineChart(container) {
  const canvas = container.querySelector('canvas');
  const readout = container.querySelector('.chart-readout');
  const context = canvas.getContext('2d');
  const colors = getComputedStyle(canvas);
  const shortNumber = new Intl.NumberFormat('zh-CN', {notation: 'compact', maximumFractionDigits: 1});
  const exactNumber = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2});
  let labels = [];
  let series = [];
  let signature;
  let selected = null;
  let plot;

  function draw() {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height || !labels.length) return;
    const scale = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * scale)) canvas.width = Math.round(width * scale);
    if (canvas.height !== Math.round(height * scale)) canvas.height = Math.round(height * scale);
    context.setTransform(scale, 0, 0, scale, 0, 0);
    context.clearRect(0, 0, width, height);

    const top = 13, bottom = height - 24, left = 44, right = width - 15;
    const high = Math.max(...series.flatMap(line => line.values));
    const integerTicks = series.every(line => line.counts);
    const rough = Math.max(integerTicks ? 1 : 0, high / 4 || 0.25);
    const power = 10 ** Math.floor(Math.log10(rough));
    const step = (integerTicks ? [1, 2, 5, 10] : [1, 2, 2.5, 5, 10]).find(unit => unit * power >= rough) * power;
    const maximum = step * 4;
    const x = index => left + index * (right - left) / Math.max(1, labels.length - 1);
    const y = value => bottom - (bottom - top) * value / maximum;
    plot = {left, right};
    context.font = colors.getPropertyValue('--type-small').trim() + ' system-ui, sans-serif';
    context.fillStyle = '#586d64';
    context.strokeStyle = '#e6ece8';
    context.lineWidth = 1;
    context.textAlign = 'right';
    context.textBaseline = 'middle';
    for (let tick = 0; tick <= 4; tick++) {
      const at = y(tick * step);
      context.beginPath();
      context.moveTo(left, at);
      context.lineTo(right, at);
      context.stroke();
      context.fillText(shortNumber.format(tick * step), left - 7, at);
    }
    context.textAlign = 'center';
    const tickCount = Math.min(labels.length, width < 370 ? 4 : 6);
    for (let tick = 0; tick < tickCount; tick++) {
      const index = Math.round(tick * (labels.length - 1) / Math.max(1, tickCount - 1));
      context.fillText(labels[index].short, Math.max(20, Math.min(width - 19, x(index))), bottom + 14);
    }
    for (const line of series) {
      const color = colors.getPropertyValue('--chart-' + line.tone).trim();
      const path = new Path2D();
      line.values.forEach((value, i) => i === 0 ? path.moveTo(x(i), y(value)) : path.lineTo(x(i), y(value)));
      if (line.area) {
        const area = new Path2D(path);
        area.lineTo(right, bottom);
        area.lineTo(left, bottom);
        area.closePath();
        const fill = context.createLinearGradient(0, top, 0, bottom);
        fill.addColorStop(0, color + '38');
        fill.addColorStop(1, color + '09');
        context.fillStyle = fill;
        context.fill(area);
      }
      context.strokeStyle = color;
      context.lineWidth = 2;
      context.lineJoin = 'round';
      context.stroke(path);
    }
    if (selected == null) return;
    context.strokeStyle = '#a2b0a8';
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(x(selected), top);
    context.lineTo(x(selected), bottom);
    context.stroke();
    for (const line of series) {
      context.fillStyle = colors.getPropertyValue('--chart-' + line.tone).trim();
      context.beginPath();
      context.arc(x(selected), y(line.values[selected]), 3.6, 0, 2 * Math.PI);
      context.fill();
    }
  }

  function inspect(index) {
    selected = index;
    readout.textContent = index == null
      ? '懸停或用方向鍵查看時段詳情'
      : labels[index].full + ' · ' + series.map(line => line.label + ' ' + exactNumber.format(line.values[index])).join(' · ');
    draw();
  }
  canvas.addEventListener('pointermove', event => {
    if (!plot || !labels.length) return;
    const fraction = (event.offsetX - plot.left) / (plot.right - plot.left);
    const index = Math.round(Math.max(0, Math.min(1, fraction)) * (labels.length - 1));
    if (index !== selected) inspect(index);
  });
  canvas.addEventListener('pointerleave', () => { if (document.activeElement !== canvas) inspect(null); });
  canvas.addEventListener('focus', () => { if (labels.length && selected == null) inspect(labels.length - 1); });
  canvas.addEventListener('blur', () => inspect(null));
  canvas.addEventListener('keydown', event => {
    if (!labels.length || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const last = labels.length - 1;
    const current = selected ?? last;
    const index = {ArrowLeft: Math.max(0, current - 1), ArrowRight: Math.min(last, current + 1), Home: 0, End: last}[event.key];
    inspect(index);
  });
  new ResizeObserver(draw).observe(canvas);

  return (nextLabels, nextSeries) => {
    labels = nextLabels;
    series = nextSeries;
    const next = JSON.stringify([labels.map(label => label.short), series]);
    if (next === signature) {
      if (selected != null) inspect(selected);
      return;
    }
    signature = next;
    inspect(selected == null ? null : Math.min(selected, labels.length - 1));
  };
}
