const ZONE_COLOR = {
  "低估": "#2e9e5b", "合理偏低": "#7cb342",
  "合理偏高": "#f0a030", "高估": "#d9453a"
};

Page({
  data: { d: null },

  onLoad(opt) {
    this.code = opt.code;
    wx.request({
      url: getApp().globalData.apiBase + "/api/indices/" + opt.code,
      success: (res) => {
        const d = res.data;
        d.compositePct = (d.composite * 100).toFixed(1);
        d.pePct = (d.pe_percentile * 100).toFixed(1);
        d.pbPct = (d.pb_percentile * 100).toFixed(1);
        d.zoneColor = ZONE_COLOR[d.zone] || "#999";
        d.yoyText = d.earnings_g == null ? "--" : (d.earnings_g * 100).toFixed(1) + "%";
        d.yoyColor = d.earnings_g < 0 ? "#d9453a" : "#2e9e5b";
        d.wPePct = Math.round(d.w_pe * 100);
        d.wPbPct = Math.round(d.w_pb * 100);
        wx.setNavigationBarTitle({ title: d.name + " 估值" });
        this.setData({ d }, () => this.drawChart(d.history));
      },
      fail: () => wx.showToast({ title: "加载失败", icon: "none" })
    });
  },

  drawChart(history) {
    if (!history || !history.length) return;
    const query = this.createSelectorQuery();
    query.select("#chart").boundingClientRect((rect) => {
      const W = rect.width, H = rect.height;
      const ctx = wx.createCanvasContext("chart", this);
      const PL = 36, PR = 40, PT = 16, PB = 24;
      const iw = W - PL - PR, ih = H - PT - PB;
      const n = history.length;
      const xs = i => PL + (i / (n - 1)) * iw;
      const ys = v => PT + (1 - v) * ih;
      const closes = history.map(h => h.close);
      const cMin = Math.min(...closes), cMax = Math.max(...closes);
      const yc = v => PT + (1 - (v - cMin) / (cMax - cMin || 1)) * ih;

      // 背景分区
      const bands = [[0, .2, "rgba(46,158,91,.10)"], [.2, .5, "rgba(124,179,66,.06)"],
                     [.5, .8, "rgba(240,160,48,.08)"], [.8, 1, "rgba(217,69,58,.10)"]];
      bands.forEach(([a, b, col]) => {
        ctx.setFillStyle(col);
        ctx.fillRect(PL, ys(b), iw, ys(a) - ys(b));
      });
      // 参考线
      [.2, .5, .8].forEach(v => {
        ctx.setStrokeStyle("#aab0bd"); ctx.setLineDash([4, 4]); ctx.setLineWidth(1);
        ctx.beginPath(); ctx.moveTo(PL, ys(v)); ctx.lineTo(W - PR, ys(v)); ctx.stroke();
        ctx.setFillStyle("#8a919e"); ctx.setFontSize(10);
        ctx.setTextAlign("right"); ctx.fillText(v * 100 + "%", PL - 4, ys(v) + 3);
      });
      // 点位曲线
      ctx.setLineDash([]); ctx.setStrokeStyle("#c9d2e0"); ctx.setLineWidth(1.5);
      ctx.beginPath();
      history.forEach((h, i) => i ? ctx.lineTo(xs(i), yc(h.close)) : ctx.moveTo(xs(0), yc(h.close)));
      ctx.stroke();
      // 综合百分位曲线
      ctx.setStrokeStyle("#3b6ef0"); ctx.setLineWidth(2);
      ctx.beginPath();
      history.forEach((h, i) => i ? ctx.lineTo(xs(i), ys(h.composite)) : ctx.moveTo(xs(0), ys(h.composite)));
      ctx.stroke();
      // 端点标注
      const last = history[n - 1];
      ctx.setFillStyle("#3b6ef0");
      ctx.beginPath(); ctx.arc(xs(n - 1), ys(last.composite), 3.5, 0, 2 * Math.PI); ctx.fill();
      ctx.setFontSize(11); ctx.setTextAlign("right");
      ctx.fillText((last.composite * 100).toFixed(1) + "%", xs(n - 1) - 6, ys(last.composite) - 8);
      // X轴年份
      ctx.setTextAlign("center"); ctx.setFontSize(10); ctx.setFillStyle("#8a919e");
      const years = {};
      history.forEach((h, i) => { const y = h.date.slice(0, 4); if (!(y in years)) years[y] = i; });
      Object.entries(years).forEach(([y, i]) => ctx.fillText(y, xs(i), H - 6));
      // 右轴点位
      ctx.setTextAlign("left");
      [cMin, (cMin + cMax) / 2, cMax].forEach(v =>
        ctx.fillText(String(Math.round(v)), W - PR + 4, yc(v) + 3));
      ctx.draw();
    }).exec();
  }
});
