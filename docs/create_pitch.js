const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
        Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
        ShadingType, PageBreak, PageNumber, ExternalHyperlink } = require('docx');
const fs = require('fs');

const TEAL = "00C9A7";
const PURPLE = "7B61FF";
const DARK_BG = "0A0C14";
const SURFACE = "111520";
const MUTED = "888888";
const WHITE = "E8E8E8";
const PINK = "FF4B6E";
const AMBER = "F5A623";

const border = { style: BorderStyle.SINGLE, size: 1, color: "333333" };
const borders = { top: border, bottom: border, left: border, right: border };
const noBorder = { style: BorderStyle.NONE, size: 0 };
const noBorders = { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder };

function heading(text, level = HeadingLevel.HEADING_1) {
  return new Paragraph({
    heading: level,
    spacing: { before: 360, after: 200 },
    children: [new TextRun({ text, bold: true, font: "Arial", size: level === HeadingLevel.HEADING_1 ? 36 : 28, color: TEAL })]
  });
}

function body(text, opts = {}) {
  return new Paragraph({
    spacing: { after: 160 },
    children: [new TextRun({ text, font: "Arial", size: 22, color: opts.color || WHITE, bold: opts.bold || false, italics: opts.italics || false })]
  });
}

function boldBody(parts) {
  return new Paragraph({
    spacing: { after: 160 },
    children: parts.map(p => new TextRun({ text: p.text, font: "Arial", size: 22, color: p.color || WHITE, bold: p.bold || false }))
  });
}

function bullet(text, boldPrefix = null) {
  const children = [];
  if (boldPrefix) {
    children.push(new TextRun({ text: boldPrefix, font: "Arial", size: 22, color: TEAL, bold: true }));
    children.push(new TextRun({ text: " \u2014 " + text, font: "Arial", size: 22, color: WHITE }));
  } else {
    children.push(new TextRun({ text: text, font: "Arial", size: 22, color: WHITE }));
  }
  return new Paragraph({
    spacing: { after: 100 },
    indent: { left: 360 },
    children: [new TextRun({ text: "\u2022 ", font: "Arial", size: 22, color: TEAL }), ...children]
  });
}

function numberedItem(num, title, desc) {
  return new Paragraph({
    spacing: { after: 120 },
    indent: { left: 360 },
    children: [
      new TextRun({ text: num + ". ", font: "Arial", size: 22, color: PURPLE, bold: true }),
      new TextRun({ text: title, font: "Arial", size: 22, color: TEAL, bold: true }),
      new TextRun({ text: " \u2014 " + desc, font: "Arial", size: 22, color: WHITE }),
    ]
  });
}

function tableCell(text, opts = {}) {
  return new TableCell({
    borders,
    width: { size: opts.width || 1872, type: WidthType.DXA },
    shading: { fill: opts.fill || SURFACE, type: ShadingType.CLEAR },
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: [new Paragraph({
      children: [new TextRun({ text, font: "Arial", size: 18, color: opts.color || WHITE, bold: opts.bold || false })]
    })]
  });
}

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 22, color: WHITE } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: "Arial", color: TEAL },
        paragraph: { spacing: { before: 360, after: 200 } } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, font: "Arial", color: PURPLE },
        paragraph: { spacing: { before: 240, after: 160 } } },
    ]
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      }
    },
    headers: {
      default: new Header({
        children: [new Paragraph({
          alignment: AlignmentType.RIGHT,
          children: [new TextRun({ text: "SYNTHOS", font: "Arial", size: 16, color: MUTED, bold: true })]
        })]
      })
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({ text: "synth-cloud.com  |  ", font: "Arial", size: 16, color: MUTED }),
            new TextRun({ text: "Page ", font: "Arial", size: 16, color: MUTED }),
            new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 16, color: MUTED }),
          ]
        })]
      })
    },
    children: [
      // TITLE PAGE
      new Paragraph({ spacing: { before: 3000 } }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: "SYNTHOS", font: "Arial", size: 72, bold: true, color: TEAL })]
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 400 },
        children: [new TextRun({ text: "Algorithmic Trading, Simplified", font: "Arial", size: 32, color: PURPLE })]
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 200 },
        children: [new TextRun({ text: "Your money. Your brokerage. Our intelligence.", font: "Arial", size: 24, color: MUTED, italics: true })]
      }),
      new Paragraph({ children: [new PageBreak()] }),

      // THE PROBLEM
      heading("The Problem"),
      body("Most retail investors are stuck choosing between two bad options:"),
      bullet("spend hours reading news, analyzing charts, and timing trades. Miss opportunities while you\u2019re at work, asleep, or living your life.", "Do it yourself"),
      bullet("give your money to a robo-advisor or fund manager who charges fees on your entire balance, makes generic decisions, and gives you zero visibility into why.", "Hand over control"),
      body("There\u2019s a better way."),

      // WHAT SYNTHOS DOES
      heading("What Synthos Does"),
      body("Synthos is an AI-powered trading agent that connects to YOUR brokerage account and makes intelligent trading decisions on your behalf \u2014 24/7, during market hours."),
      boldBody([
        { text: "You keep full control. ", bold: true, color: TEAL },
        { text: "Synthos never holds your money. Your funds stay in your Alpaca brokerage account (SIPC insured, regulated). Synthos reads market data, scores opportunities through a 14-gate decision engine, and places trades through your account\u2019s API." }
      ]),
      body("You can see every decision, override any trade, and disconnect at any time."),

      // HOW IT WORKS
      heading("How It Works"),
      numberedItem("1", "Connect", "Link your Alpaca brokerage account with API keys (2 minutes)"),
      numberedItem("2", "Configure", "Choose your risk profile: Conservative, Moderate, or Aggressive. Or customize every parameter."),
      numberedItem("3", "Watch", "The agent scans news, analyzes sentiment, screens sectors, and executes trades automatically"),
      numberedItem("4", "Control", "Kill switch, daily loss limits, position caps, and approval mode give you as much or as little oversight as you want"),

      // INTELLIGENCE STACK
      heading("The Intelligence Stack"),
      bullet("Scans and classifies market news through a 22-gate pipeline in real-time", "News Agent"),
      bullet("Monitors market mood, detects cascade risks and deterioration patterns", "Sentiment Agent"),
      bullet("Identifies the strongest sectors and ranks candidates by momentum", "Sector Screener"),
      bullet("14-gate decision spine: benchmark, regime, eligibility, confidence scoring, entry timing, position sizing, risk management, portfolio limits, and adaptive evaluation", "Trade Agent"),
      body("Every decision is logged. Every gate is auditable. No black boxes.", { bold: true }),

      new Paragraph({ children: [new PageBreak()] }),

      // COMPARISON TABLE
      heading("What Makes Synthos Different"),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [1872, 1872, 1872, 1872, 1872],
        rows: [
          new TableRow({ children: [
            tableCell("", { fill: "0A0C14", bold: true }),
            tableCell("Robo-Advisor", { fill: "1A1A2E", bold: true, color: MUTED }),
            tableCell("Signal Service", { fill: "1A1A2E", bold: true, color: MUTED }),
            tableCell("Synthos", { fill: "1A1A2E", bold: true, color: TEAL }),
          ]}),
          new TableRow({ children: [
            tableCell("Holds your money", { bold: true }),
            tableCell("Yes \u2014 they are the brokerage"),
            tableCell("No"),
            tableCell("No \u2014 your Alpaca account", { color: TEAL }),
          ]}),
          new TableRow({ children: [
            tableCell("Executes trades", { bold: true }),
            tableCell("Yes \u2014 generic"),
            tableCell("No \u2014 manual"),
            tableCell("Yes \u2014 automated", { color: TEAL }),
          ]}),
          new TableRow({ children: [
            tableCell("Transparency", { bold: true }),
            tableCell("Low"),
            tableCell("Medium"),
            tableCell("Full \u2014 every gate logged", { color: TEAL }),
          ]}),
          new TableRow({ children: [
            tableCell("Customization", { bold: true }),
            tableCell("Risk score 1-10"),
            tableCell("None"),
            tableCell("13+ parameters", { color: TEAL }),
          ]}),
          new TableRow({ children: [
            tableCell("Kill switch", { bold: true }),
            tableCell("No"),
            tableCell("N/A"),
            tableCell("Yes \u2014 instant halt", { color: TEAL }),
          ]}),
          new TableRow({ children: [
            tableCell("Monthly cost", { bold: true }),
            tableCell("~$20/mo on $100k"),
            tableCell("$77-167/mo"),
            tableCell("$50/mo flat", { color: TEAL }),
          ]}),
        ]
      }),

      // PRICING
      heading("Pricing"),
      boldBody([
        { text: "Early Adopter: $30/month", bold: true, color: PURPLE },
        { text: " (limited availability)" }
      ]),
      boldBody([
        { text: "Standard: $50/month", bold: true, color: TEAL },
      ]),
      body("Flat monthly fee. No percentage of assets. No hidden charges. No contracts \u2014 cancel anytime."),
      body("Paper trading mode included free \u2014 test the system with simulated trades before risking a dollar."),

      // SECURITY
      heading("Security & Control"),
      bullet("Your funds never leave your SIPC-insured Alpaca brokerage account"),
      bullet("API keys are encrypted at rest (AES-256 via Fernet)"),
      bullet("Two-factor authentication supported"),
      bullet("Per-account kill switch \u2014 halt all trading instantly"),
      bullet("Daily loss limits, max drawdown protection, position size caps"),
      bullet("Managed mode available \u2014 every trade requires your approval before execution"),

      // GETTING STARTED
      heading("Getting Started"),
      numberedItem("1", "Create", "your Synthos account at synth-cloud.com"),
      numberedItem("2", "Set up", "your free Alpaca paper trading account"),
      numberedItem("3", "Connect", "your API keys"),
      numberedItem("4", "Choose", "your trading style"),
      numberedItem("5", "Watch", "the agent work \u2014 in paper mode, zero risk"),

      new Paragraph({ spacing: { before: 600 } }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { after: 200 },
        children: [new TextRun({ text: "Your money. Your brokerage. Our intelligence.", font: "Arial", size: 28, bold: true, color: TEAL, italics: true })]
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new ExternalHyperlink({
          children: [new TextRun({ text: "synth-cloud.com", font: "Arial", size: 24, color: PURPLE, underline: {} })],
          link: "https://synth-cloud.com",
        })]
      }),
    ]
  }]
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync("Synthos_Sales_Pitch.docx", buffer);
  console.log("Created: Synthos_Sales_Pitch.docx (" + buffer.length + " bytes)");
});
