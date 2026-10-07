import type { ReactNode } from "react";

// ============================================================
// A SMALL MARKDOWN RENDERER
//
// Covers what the analyst writes: paragraphs, headings, bold, italics,
// inline code, links, lists (one level of nesting), tables, code blocks,
// quotes and rules. It builds React elements, never HTML strings, so an
// answer can't inject markup.
// ============================================================

interface List {
  ordered: boolean;
  start: number;
  items: { text: string; sub: List | null }[];
}

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; list: List }
  | { kind: "table"; head: string[]; align: (Align | undefined)[]; rows: string[][] }
  | { kind: "code"; text: string }
  | { kind: "quote"; text: string }
  | { kind: "rule" };

type Align = "left" | "center" | "right";

const LIST_ITEM = /^(\s*)(?:[-*+]|(\d+)[.)])\s+(.*)$/;
const HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const NUMERIC = /^[−+-]?[$≈~]?[\d,.]+\s?(%|×|x|h|k|M)?$/;

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isTableStart(lines: string[], i: number): boolean {
  return lines[i].includes("|") && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1]);
}

function startsBlock(lines: string[], i: number): boolean {
  const line = lines[i];
  return (
    line.trim().startsWith("```") ||
    HEADING.test(line) ||
    RULE.test(line) ||
    LIST_ITEM.test(line) ||
    line.startsWith(">") ||
    isTableStart(lines, i)
  );
}

export function parseBlocks(markdown: string): Block[] {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i++;
    } else if (line.trim().startsWith("```")) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) body.push(lines[i++]);
      i++;
      blocks.push({ kind: "code", text: body.join("\n") });
    } else if (HEADING.test(line)) {
      const [, hashes, text] = HEADING.exec(line)!;
      blocks.push({ kind: "heading", level: hashes.length, text });
      i++;
    } else if (RULE.test(line)) {
      blocks.push({ kind: "rule" });
      i++;
    } else if (isTableStart(lines, i)) {
      const head = cells(line);
      const align = cells(lines[i + 1]).map((cell): Align | undefined =>
        cell.startsWith(":") && cell.endsWith(":") ? "center" : cell.endsWith(":") ? "right" : cell.startsWith(":") ? "left" : undefined,
      );
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      blocks.push({ kind: "table", head, align, rows });
    } else if (LIST_ITEM.test(line)) {
      const [, , number] = LIST_ITEM.exec(line)!;
      const list: List = { ordered: number !== undefined, start: Number(number ?? 1), items: [] };

      while (i < lines.length) {
        const item = LIST_ITEM.exec(lines[i]);
        const last = list.items[list.items.length - 1];
        if (item && item[1].length >= 2 && last) {
          // Indented: a sub-item of the previous item
          last.sub ??= { ordered: item[2] !== undefined, start: Number(item[2] ?? 1), items: [] };
          last.sub.items.push({ text: item[3], sub: null });
        } else if (item && (item[2] !== undefined) === list.ordered) {
          list.items.push({ text: item[3], sub: null });
        } else if (!item && lines[i].trim() && /^\s{2,}/.test(lines[i]) && last) {
          // A continuation line
          const target = last.sub ? last.sub.items[last.sub.items.length - 1] : last;
          target.text += `\n${lines[i].trim()}`;
        } else {
          break;
        }
        i++;
      }
      blocks.push({ kind: "list", list });
    } else if (line.startsWith(">")) {
      const body: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) body.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push({ kind: "quote", text: body.join("\n") });
    } else {
      const body: string[] = [];
      while (i < lines.length && lines[i].trim() && (body.length === 0 || !startsBlock(lines, i))) body.push(lines[i++].trim());
      blocks.push({ kind: "paragraph", text: body.join("\n") });
    }
  }

  return blocks;
}

// ============================================================
// INLINE
// ============================================================

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*)|(\*[^*\s][^*\n]*?\*)|(\[[^\]\n]+\]\([^)\s]+\))/g;

function withBreaks(text: string, key: string): ReactNode[] {
  return text.split("\n").flatMap((part, i) => (i === 0 ? [part] : [<br key={`${key}-br${i}`} />, part]));
}

/** Inline markdown: `code`, **bold**, *italics*, [links](https://...) and line breaks */
export function Inline({ text }: { text: string }) {
  return <>{inline(text, "i")}</>;
}

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let index = 0;

  for (const match of text.matchAll(INLINE)) {
    const at = match.index ?? 0;
    if (at > last) out.push(...withBreaks(text.slice(last, at), `${key}-${index}t`));
    const token = match[0];
    const k = `${key}-${index++}`;

    if (match[1]) {
      out.push(<code key={k}>{token.slice(1, -1)}</code>);
    } else if (match[2]) {
      out.push(<strong key={k}>{inline(token.slice(2, -2), k)}</strong>);
    } else if (match[3]) {
      out.push(<em key={k}>{inline(token.slice(1, -1), k)}</em>);
    } else {
      const [, label, href] = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token)!;
      if (/^https?:\/\//.test(href)) {
        out.push(
          <a key={k} href={href} target="_blank" rel="noopener noreferrer">
            {label}
          </a>,
        );
      } else if (href.startsWith("#/")) {
        out.push(
          <a key={k} href={href}>
            {label}
          </a>,
        );
      } else {
        out.push(label);
      }
    }
    last = at + token.length;
  }

  if (last < text.length) out.push(...withBreaks(text.slice(last), `${key}-end`));
  return out;
}

// ============================================================
// BLOCKS
// ============================================================

function ListView({ list }: { list: List }) {
  const items = list.items.map((item, i) => (
    <li key={i}>
      <Inline text={item.text} />
      {item.sub && <ListView list={item.sub} />}
    </li>
  ));
  return list.ordered ? <ol start={list.start}>{items}</ol> : <ul>{items}</ul>;
}

function TableView({ head, align, rows }: Extract<Block, { kind: "table" }>) {
  // Columns of numbers line up on the right unless the table says otherwise; the
  // first column names the rows (part Ids, stations), so it stays on the left
  const numeric = head.map((_, c) => c > 0 && rows.length > 0 && rows.every((row) => !row[c] || NUMERIC.test(row[c].replace(/\*/g, ""))));
  const className = (c: number) => (align[c] === "right" || (align[c] === undefined && numeric[c]) ? "num" : undefined);
  const style = (c: number) => (align[c] === "center" ? { textAlign: "center" as const } : undefined);

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {head.map((cell, c) => (
              <th key={c} className={className(c)} style={style(c)}>
                <Inline text={cell} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r}>
              {head.map((_, c) => (
                <td key={c} className={className(c)} style={style(c)}>
                  <Inline text={row[c] ?? ""} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Markdown({ text, className = "" }: { text: string; className?: string }) {
  return (
    <div className={`md ${className}`.trim()}>
      {parseBlocks(text).map((block, i) => {
        switch (block.kind) {
          case "heading": {
            const Tag = block.level <= 2 ? "h3" : "h4";
            return (
              <Tag key={i}>
                <Inline text={block.text} />
              </Tag>
            );
          }
          case "paragraph":
            return (
              <p key={i}>
                <Inline text={block.text} />
              </p>
            );
          case "list":
            return <ListView key={i} list={block.list} />;
          case "table":
            return <TableView key={i} {...block} />;
          case "code":
            return <pre key={i}>{block.text}</pre>;
          case "quote":
            return (
              <blockquote key={i}>
                <Inline text={block.text} />
              </blockquote>
            );
          case "rule":
            return <hr key={i} />;
        }
      })}
    </div>
  );
}
