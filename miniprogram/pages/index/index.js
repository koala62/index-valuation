const ZONE_COLOR = {
  "低估": "#2e9e5b", "合理偏低": "#7cb342",
  "合理偏高": "#f0a030", "高估": "#d9453a"
};

Page({
  data: { list: [], loading: true, dateInfo: "" },

  onShow() { this.load(); },

  load() {
    this.setData({ loading: true });
    wx.request({
      url: getApp().globalData.apiBase + "/api/indices",
      success: (res) => {
        const list = (res.data.data || []).filter(d => !d.error).map(d => ({
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
          list, loading: false,
          dateInfo: list.length ? "数据日期 " + list[0].date : ""
        });
      },
      fail: () => {
        this.setData({ loading: false });
        wx.showToast({ title: "请求失败，请检查后端地址", icon: "none" });
      }
    });
  },

  goDetail(e) {
    wx.navigateTo({ url: "/pages/detail/detail?code=" + e.currentTarget.dataset.code });
  },

  onPullDownRefresh() {
    this.load();
    wx.stopPullDownRefresh();
  }
});
