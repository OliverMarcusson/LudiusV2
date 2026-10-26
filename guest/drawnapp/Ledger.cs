// A custom-drawn app for the visual fallback: everything inside the window is painted, so UIA sees one empty pane.
// Dark and dense on purpose (icon-only toolbar, a small-type table), the cases OCR alone handles worst.
// Repo-owned test fixture. Builds with the .NET Framework compiler every Windows 10/11 has:
//   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /target:winexe /out:Ledger.exe Ledger.cs
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

class Ledger : Form
{
    static readonly Color Back = Color.FromArgb(30, 30, 34), Panel = Color.FromArgb(44, 44, 50), Ink = Color.FromArgb(220, 220, 225);
    static readonly Color Dim = Color.FromArgb(150, 150, 160), Accent = Color.FromArgb(70, 130, 220);
    readonly Rectangle save = new Rectangle(560, 560, 90, 30), cancel = new Rectangle(660, 560, 90, 30);
    string status = "Ready";

    Ledger()
    {
        Text = "Ledger";
        ClientSize = new Size(780, 610);
        StartPosition = FormStartPosition.Manual;
        Location = new Point(240, 120);
        DoubleBuffered = true;
        BackColor = Back;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
    }

    static Rectangle Tool(int i) { return new Rectangle(12 + i * 36, 10, 30, 30); }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
        using (var panel = new SolidBrush(Panel)) g.FillRectangle(panel, 0, 0, ClientSize.Width, 50);
        using (var pen = new Pen(Ink, 2))
        using (var ink = new SolidBrush(Ink))
        {
            // Icon-only toolbar: new, open, save, print, cut, copy, paste, undo, redo, search, filter, settings.
            for (int i = 0; i < 12; i++)
            {
                var r = Tool(i);
                var c = new Point(r.X + 15, r.Y + 15);
                switch (i)
                {
                    case 0: g.DrawRectangle(pen, c.X - 7, c.Y - 9, 14, 18); g.DrawLine(pen, c.X, c.Y - 4, c.X, c.Y + 4); g.DrawLine(pen, c.X - 4, c.Y, c.X + 4, c.Y); break;
                    case 1: g.DrawRectangle(pen, c.X - 9, c.Y - 5, 18, 12); g.DrawLine(pen, c.X - 9, c.Y - 5, c.X - 4, c.Y - 9); break;
                    case 2: g.DrawRectangle(pen, c.X - 8, c.Y - 8, 16, 16); g.FillRectangle(ink, c.X - 4, c.Y - 8, 8, 5); break;
                    case 3: g.DrawRectangle(pen, c.X - 9, c.Y - 3, 18, 9); g.DrawRectangle(pen, c.X - 5, c.Y - 9, 10, 6); break;
                    case 4: g.DrawEllipse(pen, c.X - 8, c.Y + 2, 6, 6); g.DrawEllipse(pen, c.X + 2, c.Y + 2, 6, 6); g.DrawLine(pen, c.X - 4, c.Y + 2, c.X + 5, c.Y - 9); g.DrawLine(pen, c.X + 4, c.Y + 2, c.X - 5, c.Y - 9); break;
                    case 5: g.DrawRectangle(pen, c.X - 8, c.Y - 8, 11, 13); g.DrawRectangle(pen, c.X - 3, c.Y - 3, 11, 13); break;
                    case 6: g.DrawRectangle(pen, c.X - 7, c.Y - 7, 14, 16); g.FillRectangle(ink, c.X - 3, c.Y - 9, 6, 4); break;
                    case 7: g.DrawArc(pen, c.X - 7, c.Y - 6, 14, 14, 180, 220); g.DrawLine(pen, c.X - 7, c.Y + 1, c.X - 9, c.Y - 4); break;
                    case 8: g.DrawArc(pen, c.X - 7, c.Y - 6, 14, 14, 140, -220); g.DrawLine(pen, c.X + 7, c.Y + 1, c.X + 9, c.Y - 4); break;
                    case 9: g.DrawEllipse(pen, c.X - 8, c.Y - 8, 11, 11); g.DrawLine(pen, c.X + 1, c.Y + 1, c.X + 8, c.Y + 8); break;
                    case 10: g.DrawPolygon(pen, new[] { new Point(c.X - 9, c.Y - 7), new Point(c.X + 9, c.Y - 7), new Point(c.X + 2, c.Y + 1), new Point(c.X + 2, c.Y + 8), new Point(c.X - 2, c.Y + 8), new Point(c.X - 2, c.Y + 1) }); break;
                    case 11: g.DrawEllipse(pen, c.X - 8, c.Y - 8, 16, 16); g.FillEllipse(ink, c.X - 3, c.Y - 3, 6, 6); break;
                }
            }
        }
        using (var small = new Font("Segoe UI", 8f))
        using (var bold = new Font("Segoe UI", 8f, FontStyle.Bold))
        using (var ink = new SolidBrush(Ink))
        using (var dim = new SolidBrush(Dim))
        using (var grid = new Pen(Color.FromArgb(60, 60, 68)))
        {
            string[] head = { "Date", "Account", "Description", "Debit", "Credit" };
            int[] col = { 12, 100, 220, 520, 640 };
            for (int c = 0; c < head.Length; c++) g.DrawString(head[c], bold, ink, col[c], 60);
            string[] accounts = { "1930 Bank", "2440 Payables", "3010 Sales", "4010 Purchases", "5010 Rent", "6110 Office" };
            for (int r = 0; r < 24; r++)
            {
                int y = 80 + r * 19;
                g.DrawLine(grid, 12, y - 2, 760, y - 2);
                g.DrawString(string.Format("2026-10-{0:00}", r + 1), small, dim, col[0], y);
                g.DrawString(accounts[r % accounts.Length], small, ink, col[1], y);
                g.DrawString("Invoice " + (4100 + r * 7), small, ink, col[2], y);
                g.DrawString(r % 2 == 0 ? (1200 + r * 37).ToString("N2") : "", small, ink, col[3], y);
                g.DrawString(r % 2 == 1 ? (980 + r * 41).ToString("N2") : "", small, ink, col[4], y);
            }
            g.DrawString(status, small, dim, 12, 568);
        }
        using (var accent = new SolidBrush(Accent))
        using (var panel = new SolidBrush(Panel))
        using (var font = new Font("Segoe UI", 9f))
        using (var ink = new SolidBrush(Ink))
        {
            g.FillRectangle(accent, save);
            g.FillRectangle(panel, cancel);
            var mid = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
            g.DrawString("Save", font, ink, save, mid);
            g.DrawString("Cancel", font, ink, cancel, mid);
        }
    }

    protected override void OnMouseClick(MouseEventArgs e)
    {
        if (save.Contains(e.Location)) status = "Saved";
        else if (cancel.Contains(e.Location)) status = "Cancelled";
        else for (int i = 0; i < 12; i++) if (Tool(i).Contains(e.Location)) status = "Tool " + (i + 1);
        Invalidate();
    }

    [STAThread]
    static void Main() { Application.EnableVisualStyles(); Application.Run(new Ledger()); }
}
