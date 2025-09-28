(function (global, factory) {
    if (typeof module === 'object' && module.exports) {
        module.exports = factory(require('chart.js'));
    } else {
        global.ChartFinancial = factory(global.Chart);
    }
}(this, function (Chart) {
    if (!Chart) {
        throw new Error('Chart.js not found. chartjs-chart-financial requires Chart.js to be available globally.');
    }

    const globalScope = typeof globalThis !== 'undefined' ? globalThis : (typeof window !== 'undefined' ? window : (typeof self !== 'undefined' ? self : {}));
    const valueOrDefault = (value, defaultValue) => (value === undefined ? defaultValue : value);

    class CandlestickElement extends Chart.Element {
        draw(ctx) {
            const {
                x,
                width,
                high,
                low,
                open,
                close,
                options
            } = this;
            if (!Number.isFinite(x) || !Number.isFinite(high) || !Number.isFinite(low)) {
                return;
            }

            const up = close >= open;
            const bodyTop = Math.min(open, close);
            const bodyBottom = Math.max(open, close);
            const bodyHeight = Math.max(Math.abs(close - open), 1);
            const halfWidth = width / 2;

            const color = up ? options.upColor : options.downColor;
            const borderColor = up ? valueOrDefault(options.upBorderColor, color) : valueOrDefault(options.downBorderColor, color);
            const wickColor = valueOrDefault(options.wickColor, borderColor);

            ctx.save();
            ctx.lineWidth = options.wickWidth;
            ctx.strokeStyle = wickColor;

            ctx.beginPath();
            ctx.moveTo(x, high);
            ctx.lineTo(x, low);
            ctx.stroke();

            ctx.fillStyle = color;
            ctx.lineWidth = options.borderWidth;
            ctx.strokeStyle = borderColor;

            const bodyX = x - halfWidth;
            const bodyY = bodyTop;

            if (bodyHeight <= options.minBodyHeight) {
                ctx.beginPath();
                ctx.moveTo(bodyX, bodyBottom);
                ctx.lineTo(bodyX + width, bodyBottom);
                ctx.stroke();
            } else {
                ctx.fillRect(bodyX, bodyTop, width, bodyHeight);
                if (options.borderWidth > 0) {
                    ctx.strokeRect(bodyX, bodyTop, width, bodyHeight);
                }
            }

            ctx.restore();
        }

        inRange(mouseX, mouseY) {
            const { x, width, high, low } = this;
            if (!Number.isFinite(x) || !Number.isFinite(high) || !Number.isFinite(low)) {
                return false;
            }
            const half = width / 2;
            return mouseX >= x - half && mouseX <= x + half && mouseY >= Math.min(high, low) && mouseY <= Math.max(high, low);
        }

        inXRange(mouseX) {
            const { x, width } = this;
            if (!Number.isFinite(x)) {
                return false;
            }
            const half = width / 2;
            return mouseX >= x - half && mouseX <= x + half;
        }

        getCenterPoint() {
            const { x, open, close } = this;
            return {
                x,
                y: (open + close) / 2
            };
        }

        tooltipPosition() {
            return this.getCenterPoint();
        }

        hasValue() {
            return Number.isFinite(this.x) && Number.isFinite(this.open) && Number.isFinite(this.close);
        }
    }

    CandlestickElement.id = 'candlestickElement';
    CandlestickElement.defaults = {
        upColor: '#26a69a',
        downColor: '#ef5350',
        upBorderColor: '#26a69a',
        downBorderColor: '#ef5350',
        wickColor: '#666666',
        wickWidth: 1,
        borderWidth: 1,
        minBodyHeight: 1,
        width: undefined
    };

    const parseValue = (scale, value, index) => {
        if (scale && typeof scale.parse === 'function') {
            return scale.parse(value, index);
        }
        return value;
    };

    class CandlestickController extends Chart.DatasetController {
        initialize() {
            super.initialize();
            this.enableOptionSharing = true;
        }

        parse(start, count) {
            const meta = this._cachedMeta;
            const data = this.getDataset().data || [];
            const parsed = meta._parsed || (meta._parsed = []);
            parsed.length = data.length;

            if (!count) {
                count = data.length - start;
            }

            const xScale = meta.xScale;

            for (let i = start; i < start + count; ++i) {
                const item = data[i] || {};
                const xValue = item.x !== undefined ? item.x : (item.t !== undefined ? item.t : i);
                const o = Number(item.o);
                const h = Number(item.h);
                const l = Number(item.l);
                const c = Number(item.c);

                parsed[i] = {
                    x: parseValue(xScale, xValue, i),
                    o,
                    h,
                    l,
                    c,
                    _custom: {
                        o,
                        h,
                        l,
                        c,
                        raw: item
                    }
                };
            }
        }

        getLabelAndValue(index) {
            const meta = this._cachedMeta;
            const parsed = meta._parsed[index];
            const labels = this.chart.data.labels || [];
            const label = labels[index] !== undefined ? labels[index] : meta.xScale?.getLabelForValue(parsed?.x);

            if (!parsed) {
                return {
                    label: label || '',
                    value: ''
                };
            }

            const format = (v) => Number.isFinite(v) ? v : '-';
            return {
                label: label || '',
                value: `O: ${format(parsed.o)}  H: ${format(parsed.h)}  L: ${format(parsed.l)}  C: ${format(parsed.c)}`,
                parsed
            };
        }

        updateElements(elements, start, count, mode) {
            const meta = this._cachedMeta;
            const parsed = meta._parsed;
            const xScale = meta.xScale;
            const yScale = meta.yScale;
            const reset = mode === 'reset';
            const includeOptions = this.includeOptions(mode);
            const sharedOptions = includeOptions ? this.getSharedOptions(mode, elements[start]) : undefined;

            for (let i = 0; i < count; ++i) {
                const index = start + i;
                const element = elements[i];
                const item = parsed[index];
                if (!item || !Number.isFinite(item.o) || !Number.isFinite(item.h) || !Number.isFinite(item.l) || !Number.isFinite(item.c)) {
                    element.skip = true;
                    continue;
                }

                const x = xScale.getPixelForValue(item.x, index);
                const width = this._calculateCandleWidth(index, item.x);
                const open = yScale.getPixelForValue(reset ? item.c : item.o);
                const close = yScale.getPixelForValue(item.c);
                const high = yScale.getPixelForValue(item.h);
                const low = yScale.getPixelForValue(item.l);

                element.skip = false;
                element.x = x;
                element.y = (open + close) / 2;
                element.open = open;
                element.close = close;
                element.high = high;
                element.low = low;
                element.width = width;
                element.base = yScale.getBasePixel();
                element.raw = item._custom?.raw || null;
                element.parsed = item;
                element.pivot();

                if (includeOptions) {
                    element.options = sharedOptions || this.resolveDataElementOptions(index, mode);
                }
            }
        }

        _calculateCandleWidth(index) {
            const meta = this._cachedMeta;
            const xScale = meta.xScale;
            const dataset = this.getDataset();
            const options = this.chart.options.elements?.candlestick || {};
            const customWidth = valueOrDefault(dataset.candleWidth, options.width);
            if (Number.isFinite(customWidth) && customWidth > 0) {
                return customWidth;
            }

            const parsed = meta._parsed;
            const current = parsed[index];
            if (!current) {
                return 6;
            }
            const prev = parsed[index - 1];
            const next = parsed[index + 1];
            let diff = 10;
            const currentPixel = xScale.getPixelForValue(current.x, index);
            if (next) {
                diff = Math.abs(xScale.getPixelForValue(next.x, index + 1) - currentPixel);
            } else if (prev) {
                diff = Math.abs(currentPixel - xScale.getPixelForValue(prev.x, index - 1));
            }
            if (!Number.isFinite(diff) || diff <= 0) {
                diff = 10;
            }
            return Math.max(3, diff * 0.6);
        }

        resolveDataElementOptions(index, mode) {
            const base = super.resolveDataElementOptions(index, mode);
            const parsed = this._cachedMeta._parsed[index];
            if (!parsed) {
                return base;
            }
            const up = parsed.c >= parsed.o;
            return Object.assign({}, base, {
                upColor: valueOrDefault(base.upColor, CandlestickElement.defaults.upColor),
                downColor: valueOrDefault(base.downColor, CandlestickElement.defaults.downColor),
                upBorderColor: valueOrDefault(base.upBorderColor, valueOrDefault(base.borderColor, CandlestickElement.defaults.upBorderColor)),
                downBorderColor: valueOrDefault(base.downBorderColor, valueOrDefault(base.borderColor, CandlestickElement.defaults.downBorderColor)),
                wickColor: valueOrDefault(base.wickColor, valueOrDefault(base.borderColor, CandlestickElement.defaults.wickColor)),
                wickWidth: valueOrDefault(base.wickWidth, CandlestickElement.defaults.wickWidth),
                borderWidth: valueOrDefault(base.borderWidth, CandlestickElement.defaults.borderWidth),
                minBodyHeight: valueOrDefault(base.minBodyHeight, CandlestickElement.defaults.minBodyHeight),
                color: up ? valueOrDefault(base.upColor, CandlestickElement.defaults.upColor) : valueOrDefault(base.downColor, CandlestickElement.defaults.downColor)
            });
        }

        getMinMax(scale) {
            const meta = this._cachedMeta;
            const parsed = meta._parsed || [];
            if (!parsed.length) {
                return { min: Number.POSITIVE_INFINITY, max: Number.NEGATIVE_INFINITY };
            }
            if (scale === meta.xScale) {
                let minX = Number.POSITIVE_INFINITY;
                let maxX = Number.NEGATIVE_INFINITY;
                for (const item of parsed) {
                    if (!item) {
                        continue;
                    }
                    const x = item.x;
                    if (Number.isFinite(x)) {
                        minX = Math.min(minX, x);
                        maxX = Math.max(maxX, x);
                    }
                }
                return { min: minX, max: maxX };
            }

            let minY = Number.POSITIVE_INFINITY;
            let maxY = Number.NEGATIVE_INFINITY;
            for (const item of parsed) {
                if (!item) {
                    continue;
                }
                if (Number.isFinite(item.l)) {
                    minY = Math.min(minY, item.l);
                }
                if (Number.isFinite(item.h)) {
                    maxY = Math.max(maxY, item.h);
                }
            }
            return { min: minY, max: maxY };
        }
    }

    CandlestickController.id = 'candlestick';
    CandlestickController.defaults = {
        dataElementType: CandlestickElement,
        parsing: false,
        animations: {
            numbers: {
                properties: ['x', 'y', 'open', 'close', 'high', 'low', 'width'],
                type: 'number'
            },
            colors: {
                properties: ['color', 'borderColor', 'upColor', 'downColor', 'wickColor']
            }
        },
        transitions: {
            zoom: {
                animation: {
                    duration: 0
                }
            }
        }
    };

    CandlestickController.overrides = {
        plugins: {
            tooltip: {
                mode: 'index'
            }
        },
        parsing: false
    };

    Chart.defaults.elements = Chart.defaults.elements || {};
    Chart.defaults.elements.candlestick = Object.assign({}, CandlestickElement.defaults);

    Chart.register(CandlestickElement);
    Chart.register(CandlestickController);

    if (globalScope) {
        globalScope.ChartFinancial = globalScope.ChartFinancial || {};
        Object.assign(globalScope.ChartFinancial, { CandlestickController, CandlestickElement });
        globalScope['chartjs-chart-financial'] = globalScope.ChartFinancial;
        globalScope.chartFinancial = globalScope.ChartFinancial;
        globalScope.financialChart = globalScope.ChartFinancial;
    }

    return {
        CandlestickController,
        CandlestickElement
    };
}));
