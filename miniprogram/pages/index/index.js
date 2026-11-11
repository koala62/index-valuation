const ZONE_COLOR = {
  "低估": "#2e9e5b", "合理偏低": "#7cb342",
  "合理偏高": "#f0a030", "高估": "#d9453a"
};
const CATS = [
  { key: "all", label: "全部" },
  { key: "broad", label: "宽基" },
  { key: "strategy", label: "策略红利" },
  { key: "industry", label: "行业" }
];
const ZONES = ["all", "低估", "合理偏低", "合理偏高", "高估"];

Page({
  data: {
    list: [], raw: [], loading: true, dateInfo: "",
    cats: CATS.map(c => ({ ...c, n: 0 })),
    zones: ZONES.map(z => ({ key: z, label: z === "all" ? "全部区间" : z, n: 0 })),
    cat: "all", zone: "all", count: 0
  },

  onShow() { this.load(); },

  load() {
    this.setData({ loading: true });
    wx.request({
      url: getApp().globalData.apiBase + "/api/indices",
      success: (res) => {
        const raw = (res.data.data || []).filter(d => !d.error).map(d => ({
          ...d,
          compositePct: (d.composite * 100).toFixed(1),
          pePct: (d.pe_percentile * 100).toFixed(1),
          pbPct: (d.pb_percentile * 100).toFixed(1),
          wPePct: Math.round(d.w_pe * 100),
          wPbPct: Math.round(d.w_pb * 100),
          yoyText: d.earnings_g == null ? "--" : (d.earnings_g * 100).toFixed(1) + "%",
          zoneColor: ZONE_COLOR[d.zone] || "#999",
          pinLeft: Math.min(97, d.composite * 100)
        }));
        this.setData({
          raw, loading: false,
          dateInfo: raw.length ? "数据日期 " + raw[0].date : ""
        });
        this.applyFilter();
      },
      fail: () => {
        this.setData({ loading: false });
        wx.showToast({ title: "请求失败，请检查后端地址", icon: "none" });
      }
    });
  },

  applyFilter() {
    const { raw, cat, zone } = this.data;
    const hit = (d, skip) =>
      (skip === "cat" || cat === "all" || d.category === cat) &&
      (skip === "zone" || zone === "all" || d.zone === zone);
    const cats = CATS.map(c => ({
      ...c, n: raw.filter(d => (c.key === "all" || d.category === c.key) && hit(d, "cat")).length
    }));
    const zones = ZONES.map(z => ({
      key: z, label: z === "all" ? "全部区间" : z,
      n: raw.filter(d => (z === "all" || d.zone === z) && hit(d, "zone")).length
    }));
    const list = raw.filter(d => hit(d, null));
    this.setData({ list, cats, zones, count: list.length });
  },

  pickCat(e) {
    this.setData({ cat: e.currentTarget.dataset.key });
    this.applyFilter();
  },

  pickZone(e) {
    this.setData({ zone: e.currentTarget.dataset.key });
    this.applyFilter();
  },

  goDetail(e) {
    wx.navigateTo({ url: "/pages/detail/detail?code=" + e.currentTarget.dataset.code });
  },

  onPullDownRefresh() {
    this.load();
    wx.stopPullDownRefresh();
  }
});
