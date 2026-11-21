# The instrument layer

Looking into a machine room you own: charts over time, bars, sparklines, pictures of pages, and the board
under full-bleed instruments (`styles/instrument.css`). In the catalog: `/$org/design#instrument`.

| Use | Component | File |
|---|---|---|
| A value over time, with gaps and incident bands | `TimeChart` | `components/charts/time-chart.tsx` |
| Counts per bucket, by series | `StackedBars` | `components/charts/stacked-bars.tsx` |
| A row's last hour | `Sparkline` | `components/charts/small.tsx` |
| A chart's legend, readout and its table for screen readers | `Legend`, `Readout`, `ChartTable`, `useWidth` | `components/charts/shared.tsx` |
| A page screenshot with numbered findings | `ImageFrame` | `components/ui/image-frame.tsx` |
| Before and after | `BeforeAfter` (a wipe, or side by side) | `components/ui/before-after.tsx` |
| A full-bleed instrument | `board-surface`, `silk` labels, one `registered` panel per screen | `styles/instrument.css` |
| A picture of an environment's screen, a chip on it | `ScreenPicture`, `PictureChip` | `components/screen-picture.tsx` |

Data reads in text colour; a bar that is the point is ink (`chart-bar-strong`). Every chart has a table
behind it for screen readers. A real gap is drawn as a gap, never joined over.

## Not this

- A chart library's default palette, gradients, 3D, or a legend that needs colour to read.
- A made-up number where the node sends none: say it does not report it yet.
