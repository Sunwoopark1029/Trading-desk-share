import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { TickerLink, remarkTickers, useKnownTickers } from "../TickerLink";

/** Renders the AI's answer: bold, lists, headings, tables (GFM). Raw HTML is never rendered;
 *  links open in a new tab. Listed US tickers (Toss list) become links to their Research page. */
export default function Markdown({ text }: { text: string }) {
  const known = useKnownTickers();
  return (
    <div className="md">
      <ReactMarkdown
        // singleTilde off: "55.91~56.01" is a price range, not strikethrough (only ~~text~~ strikes)
        remarkPlugins={[[remarkGfm, { singleTilde: false }], remarkTickers(known)]}
        skipHtml
        urlTransform={(url) => (url.startsWith("ticker:") ? url : defaultUrlTransform(url))}
        components={{
          a: ({ href, children }) => (href?.startsWith("ticker:")
            ? <TickerLink sym={href.slice(7)}>{children}</TickerLink>
            : <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>),
          table: ({ children }) => <div className="md-table"><table>{children}</table></div>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
