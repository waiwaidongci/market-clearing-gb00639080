export const ROLE = {admin:'复核主管', analyst:'分析员', viewer:'只读查看'};
export const STATUS = {queued:'排队中', running:'正在计算', complete:'计算完成', failed:'运行失败'};
export const REVIEW = {pending:'待复核', accepted:'已通过', returned:'已退回'};
export const REPORT = {
  intact:'校验通过', restored:'已重建原件', missing:'报告已删除', altered:'报告被改动',
  unsealed:'无校验凭据', unknown:'待核对',
};
