// Two-stage, ready/valid command pipeline with synchronous flush.
module gpu_command_pipeline (
    input  logic        clk,
    input  logic        reset_n,
    input  logic        flush,
    input  logic        in_valid,
    output logic        in_ready,
    input  logic [15:0] in_tag,
    input  logic [1:0]  in_opcode,
    input  logic [15:0] in_a,
    input  logic [15:0] in_b,
    output logic        out_valid,
    input  logic        out_ready,
    output logic [15:0] out_tag,
    output logic [15:0] out_result,
    output logic        out_overflow
);
    logic stage1_valid, stage2_valid;
    logic [15:0] stage1_tag, stage1_a, stage1_b;
    logic [1:0] stage1_opcode;
    logic [15:0] stage2_tag, stage2_result;
    logic stage2_overflow;
    logic ready1, ready2;
    logic [16:0] computed;

    always_comb begin
        case (stage1_opcode)
            2'b00: computed = {1'b0, stage1_a} + {1'b0, stage1_b};
            2'b01: computed = {1'b0, stage1_a ^ stage1_b};
            2'b10: computed = {1'b0, stage1_a << stage1_b[3:0]};
            2'b11: computed = {1'b0, stage1_a} - {1'b0, stage1_b};
            default: computed = 17'b0;
        endcase
    end

    assign ready2 = !stage2_valid || out_ready;
    assign ready1 = !stage1_valid || ready2;
    assign in_ready = reset_n && !flush && ready1;
    assign out_valid = reset_n && !flush && stage2_valid;
    assign out_tag = stage2_tag;
    assign out_result = stage2_result;
    assign out_overflow = stage2_overflow;

    always_ff @(posedge clk) begin
        if (!reset_n || flush) begin
            stage1_valid <= 1'b0;
            stage2_valid <= 1'b0;
        end else begin
            if (ready2) begin
                stage2_valid <= stage1_valid;
                if (stage1_valid) begin
                    stage2_tag <= stage1_tag;
                    stage2_result <= computed[15:0];
                    // ADD carry and SUB borrow are represented by the high bit.
                    stage2_overflow <= (stage1_opcode == 2'b00 || stage1_opcode == 2'b11)
                        && computed[16];
                end
            end
            if (ready1) begin
                stage1_valid <= in_valid;
                if (in_valid) begin
                    stage1_tag <= in_tag;
                    stage1_opcode <= in_opcode;
                    stage1_a <= in_a;
                    stage1_b <= in_b;
                end
            end
        end
    end
endmodule
