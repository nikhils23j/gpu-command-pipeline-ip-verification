// gpu_command_pipeline -- a tiny GPU-style ALU command pipeline.
//
//   in  -> [skid buffer] -> [issue] -+-> [EX1] -> [EX2] ------+-> [reorder buffer] -> [out reg] -> out
//                                    +-> [iterative divider] -+   (in-order retire)
//
// * Ready/valid on both sides. in_ready comes straight from a flop (skid buffer),
//   so the upstream never sees a combinational path from out_ready.
// * 16 ops (4-bit opcode). Everything except DIVU/REMU has a fixed 2-cycle
//   execute latency. DIVU/REMU use a 1-bit-per-cycle restoring divider
//   (16 iterations + 1 write-back cycle), so results complete out of order.
// * Every op reserves a reorder-buffer (ROB) slot when it issues. Free slots
//   act as credits: an op only issues if it has a slot, so execution units
//   never have to stall -- the only stall points are issue and the output.
// * Results leave strictly in issue order, carrying the tag they came in with.
// * flush (synchronous, active high) and reset_n (synchronous, active low)
//   drop everything in flight. During a flush/reset cycle in_ready and
//   out_valid are forced low.
//
// Bug injection: compile with -DBUG_<NAME> to turn on one of the deliberate
// bugs below (used by scripts/mutate.py to prove the testbenches catch them).
//   BUG_SKID_DUP        skid entry not cleared when it moves to main -> op duplicated
//   BUG_ROB_NOFULL      issue ignores ROB-full -> overwrites an un-retired slot
//   BUG_OUT_NOT_HELD    output register reloads while out_valid && !out_ready
//   BUG_ADDS_CARRY      saturating add detects overflow with unsigned carry
//   BUG_SRA_LOGICAL     arithmetic shift right implemented as logical shift
//   BUG_DIV_SHORT       divider runs 15 iterations instead of 16
//   BUG_FLUSH_DIV       flush does not cancel an in-flight divide
//   BUG_MINMAX_UNSIGNED MIN/MAX compare unsigned instead of signed
//   BUG_MAC_FLAG        MAC overflow flag ignores the carry out of the add
`timescale 1ns/1ps
module gpu_command_pipeline #(
    parameter int TAG_W     = 8,
    parameter int ROB_DEPTH = 8          // must be a power of two, >= 2
) (
    input  logic             clk,
    input  logic             reset_n,
    input  logic             flush,
    // command in
    input  logic             in_valid,
    output logic             in_ready,
    input  logic [TAG_W-1:0] in_tag,
    input  logic [3:0]       in_op,
    input  logic [15:0]      in_a,
    input  logic [15:0]      in_b,
    input  logic [15:0]      in_c,
    // result out
    output logic             out_valid,
    input  logic             out_ready,
    output logic [TAG_W-1:0] out_tag,
    output logic [15:0]      out_result,
    output logic             out_flag
);
    localparam int IDX_W = (ROB_DEPTH > 1) ? $clog2(ROB_DEPTH) : 1;
    localparam int CNT_W = IDX_W + 1;

    localparam logic [3:0] OP_ADD = 4'd0,  OP_SUB = 4'd1,  OP_ADDS = 4'd2,  OP_MUL = 4'd3,
                           OP_MAC = 4'd4,  OP_SHL = 4'd5,  OP_SHR  = 4'd6,  OP_SRA = 4'd7,
                           OP_MIN = 4'd8,  OP_MAX = 4'd9,  OP_CMPLT = 4'd10, OP_AND = 4'd11,
                           OP_OR  = 4'd12, OP_XOR = 4'd13, OP_DIVU = 4'd14, OP_REMU = 4'd15;

    localparam int PW = TAG_W + 4 + 48;   // packed command width
    logic kill;
    assign kill = !reset_n || flush;

    // ------------------------------------------------------------------
    // 1. Input skid buffer (2 entries: main + skid)
    // ------------------------------------------------------------------
    logic          m_valid, s_valid;
    logic [PW-1:0] m_cmd, s_cmd, in_cmd;
    logic          in_fire, issue_fire;

    assign in_cmd   = {in_tag, in_op, in_a, in_b, in_c};
    assign in_ready = !kill && !s_valid;
    assign in_fire  = in_valid && in_ready;

    always_ff @(posedge clk) begin
        if (kill) begin
            m_valid <= 1'b0;
            s_valid <= 1'b0;
        end else if (!m_valid || issue_fire) begin
            // main slot frees up this cycle
            if (s_valid) begin
                m_cmd   <= s_cmd;
                m_valid <= 1'b1;
`ifndef BUG_SKID_DUP
                s_valid <= 1'b0;
`endif
            end else begin
                m_cmd   <= in_cmd;
                m_valid <= in_fire;
            end
        end else if (in_fire) begin
            s_cmd   <= in_cmd;
            s_valid <= 1'b1;
        end
    end

    logic [TAG_W-1:0] m_tag;
    logic [3:0]       m_op;
    logic [15:0]      m_a, m_b, m_c;
    assign {m_tag, m_op, m_a, m_b, m_c} = m_cmd;

    // ------------------------------------------------------------------
    // 2. Reorder buffer bookkeeping + issue
    // ------------------------------------------------------------------
    logic [IDX_W-1:0] rob_head, rob_tail;
    logic [CNT_W-1:0] rob_count;
    logic             rob_full, retire_fire;
    logic             div_busy;
    logic             m_is_div, issue_ok;
    logic [IDX_W-1:0] div_idx;
    logic             div_is_rem;
    logic [4:0]       div_cnt;
    logic [15:0]      div_quo, div_den, div_num;
    logic [16:0]      div_rem;
    logic [16:0]      div_trial;
    logic             div_wb;
    logic [15:0]      div_result;
    logic             div_flag;

    assign rob_full = (rob_count == ROB_DEPTH[CNT_W-1:0]);
    assign m_is_div = (m_op == OP_DIVU) || (m_op == OP_REMU);
`ifdef BUG_ROB_NOFULL
    assign issue_ok = !(m_is_div && div_busy && !div_wb);
`else
    // A new divide may issue in the same cycle the old one writes back.
    assign issue_ok = !rob_full && !(m_is_div && div_busy && !div_wb);
`endif
    assign issue_fire = !kill && m_valid && issue_ok;

    // ------------------------------------------------------------------
    // 3. Fast execute path: EX1 (ALU + multiply) -> EX2 (MAC add, flags)
    // ------------------------------------------------------------------
    logic             e1_valid, e2_valid;
    logic [IDX_W-1:0] e1_idx, e2_idx;
    logic [3:0]       e1_op, e2_op;
    logic [15:0]      e1_a, e1_b, e1_c;

    logic [16:0] add17, sub17;
    logic [15:0] adds16, shl16, shr16, sra16, min16, max16, alu16;
    logic        adds_ovf, alu_flag, a_lt_b;
    logic [31:0] prod32;

    always @* begin  // always_comb; written as @* to keep iverilog quiet
        add17  = {1'b0, e1_a} + {1'b0, e1_b};
        sub17  = {1'b0, e1_a} - {1'b0, e1_b};
        adds16 = e1_a + e1_b;
`ifdef BUG_ADDS_CARRY
        adds_ovf = add17[16];
`else
        adds_ovf = (e1_a[15] == e1_b[15]) && (adds16[15] != e1_a[15]);
`endif
        prod32 = e1_a * e1_b;
        shl16  = e1_a << e1_b[3:0];
        shr16  = e1_a >> e1_b[3:0];
`ifdef BUG_SRA_LOGICAL
        sra16  = e1_a >> e1_b[3:0];
`else
        sra16  = $signed(e1_a) >>> e1_b[3:0];
`endif
`ifdef BUG_MINMAX_UNSIGNED
        a_lt_b = e1_a < e1_b;
`else
        a_lt_b = $signed(e1_a) < $signed(e1_b);
`endif
        min16  = a_lt_b ? e1_a : e1_b;
        max16  = a_lt_b ? e1_b : e1_a;

        alu_flag = 1'b0;
        case (e1_op)
            OP_ADD:   begin alu16 = add17[15:0]; alu_flag = add17[16]; end
            OP_SUB:   begin alu16 = sub17[15:0]; alu_flag = sub17[16]; end
            OP_ADDS:  begin
                          alu16 = adds_ovf ? (e1_a[15] ? 16'h8000 : 16'h7fff) : adds16;
                          alu_flag = adds_ovf;
                      end
            OP_MUL:   begin alu16 = prod32[15:0]; alu_flag = |prod32[31:16]; end
            OP_SHL:   alu16 = shl16;
            OP_SHR:   alu16 = shr16;
            OP_SRA:   alu16 = sra16;
            OP_MIN:   alu16 = min16;
            OP_MAX:   alu16 = max16;
            OP_CMPLT: alu16 = {15'd0, ($signed(e1_a) < $signed(e1_b))};
            OP_AND:   alu16 = e1_a & e1_b;
            OP_OR:    alu16 = e1_a | e1_b;
            OP_XOR:   alu16 = e1_a ^ e1_b;
            default:  alu16 = 16'd0;   // MAC finishes in EX2; DIV never enters
        endcase
    end

    // EX2 registers
    logic [15:0] e2_alu, e2_c;
    logic        e2_flag;
    logic [31:0] e2_prod;
    logic [32:0] mac33;
    logic [15:0] e2_result;
    logic        e2_res_flag;

    always @* begin  // always_comb; written as @* to keep iverilog quiet
        mac33 = {1'b0, e2_prod} + {17'd0, e2_c};
        if (e2_op == OP_MAC) begin
            e2_result = mac33[15:0];
`ifdef BUG_MAC_FLAG
            e2_res_flag = |e2_prod[31:16];
`else
            e2_res_flag = |mac33[32:16];
`endif
        end else begin
            e2_result   = e2_alu;
            e2_res_flag = e2_flag;
        end
    end

    always_ff @(posedge clk) begin
        if (kill) begin
            e1_valid <= 1'b0;
            e2_valid <= 1'b0;
        end else begin
            e1_valid <= issue_fire && !m_is_div;
            if (issue_fire && !m_is_div) begin
                e1_idx <= rob_tail;
                e1_op  <= m_op;
                e1_a   <= m_a;
                e1_b   <= m_b;
                e1_c   <= m_c;
            end
            e2_valid <= e1_valid;
            if (e1_valid) begin
                e2_idx  <= e1_idx;
                e2_op   <= e1_op;
                e2_alu  <= alu16;
                e2_flag <= alu_flag;
                e2_prod <= prod32;
                e2_c    <= e1_c;
            end
        end
    end

    // ------------------------------------------------------------------
    // 4. Iterative restoring divider (unsigned), one quotient bit per cycle
    // ------------------------------------------------------------------
    // (divider state is declared up in section 2 because issue looks at it)

    assign div_trial  = {div_rem[15:0], div_quo[15]};
    assign div_wb     = div_busy && (div_cnt == 5'd0);
    assign div_flag   = (div_den == 16'd0);
    // A restoring divider already returns q=0xFFFF, r=a for b=0; the explicit
    // case documents the rule instead of relying on the algorithm.
    assign div_result = div_flag ? (div_is_rem ? div_num : 16'hffff)
                                 : (div_is_rem ? div_rem[15:0] : div_quo);

    always_ff @(posedge clk) begin
`ifdef BUG_FLUSH_DIV
        if (!reset_n) begin
`else
        if (kill) begin
`endif
            div_busy <= 1'b0;
        end else if (issue_fire && m_is_div) begin
            div_busy   <= 1'b1;
            div_idx    <= rob_tail;
            div_is_rem <= (m_op == OP_REMU);
            div_num    <= m_a;
            div_den    <= m_b;
            div_quo    <= m_a;
            div_rem    <= 17'd0;
`ifdef BUG_DIV_SHORT
            div_cnt    <= 5'd15;
`else
            div_cnt    <= 5'd16;
`endif
        end else if (div_busy) begin
            if (div_cnt != 5'd0) begin
                if (div_trial >= {1'b0, div_den}) begin
                    div_rem <= div_trial - {1'b0, div_den};
                    div_quo <= {div_quo[14:0], 1'b1};
                end else begin
                    div_rem <= div_trial;
                    div_quo <= {div_quo[14:0], 1'b0};
                end
                div_cnt <= div_cnt - 5'd1;
            end else begin
                div_busy <= 1'b0;          // write-back happens this cycle
            end
        end
    end

    // ------------------------------------------------------------------
    // 5. Reorder buffer storage, write-back, in-order retire
    // ------------------------------------------------------------------
    logic             rob_done [ROB_DEPTH];
    logic [TAG_W-1:0] rob_tag  [ROB_DEPTH];
    logic [15:0]      rob_res  [ROB_DEPTH];
    logic             rob_flag [ROB_DEPTH];

    logic             o_valid;
    logic [TAG_W-1:0] o_tag;
    logic [15:0]      o_res;
    logic             o_flag;
    logic             head_done;

    assign head_done = (rob_count != '0) && rob_done[rob_head];
`ifdef BUG_OUT_NOT_HELD
    assign retire_fire = !kill && head_done;
`else
    assign retire_fire = !kill && head_done && (!o_valid || out_ready);
`endif

    always_ff @(posedge clk) begin
        if (kill) begin
            rob_head  <= '0;
            rob_tail  <= '0;
            rob_count <= '0;
            for (int i = 0; i < ROB_DEPTH; i++) rob_done[i] <= 1'b0;
        end else begin
            if (issue_fire) begin
                rob_done[rob_tail] <= 1'b0;
                rob_tag[rob_tail]  <= m_tag;
                rob_tail           <= rob_tail + 1'b1;
            end
            if (e2_valid) begin
                rob_done[e2_idx] <= 1'b1;
                rob_res[e2_idx]  <= e2_result;
                rob_flag[e2_idx] <= e2_res_flag;
            end
            if (div_wb) begin
                rob_done[div_idx] <= 1'b1;
                rob_res[div_idx]  <= div_result;
                rob_flag[div_idx] <= div_flag;
            end
            if (retire_fire) begin
                rob_done[rob_head] <= 1'b0;
                rob_head           <= rob_head + 1'b1;
            end
            rob_count <= rob_count + {{(CNT_W-1){1'b0}}, issue_fire}
                                   - {{(CNT_W-1){1'b0}}, retire_fire};
        end
    end

    // Output register: holds its value while out_valid && !out_ready.
    always_ff @(posedge clk) begin
        if (kill) begin
            o_valid <= 1'b0;
        end else if (retire_fire) begin
            o_valid <= 1'b1;
            o_tag   <= rob_tag[rob_head];
            o_res   <= rob_res[rob_head];
            o_flag  <= rob_flag[rob_head];
        end else if (out_ready) begin
            o_valid <= 1'b0;
        end
    end

    assign out_valid  = !kill && o_valid;
    assign out_tag    = o_tag;
    assign out_result = o_res;
    assign out_flag   = o_flag;
endmodule
