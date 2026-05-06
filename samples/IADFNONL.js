// IADFNONL.js — example screen JS for Fund Online
// Mirrors typical FLEXCUBE patterns for the parser to exercise.

// On change of FUND_PRODUCT, fetch product defaults and populate Fund ID + Ref No
function onChangeFundProduct() {
    var prodCode = getFieldValue("BLK_FUND_HEADER", "FUND_PRODUCT");
    if (prodCode == "" || prodCode == null) {
        return;
    }
    if (prodCode.length > 4) {
        showError("Product code cannot exceed 4 characters");
        return;
    }
    setFieldValue("BLK_FUND_HEADER", "FUND_ID", generateRandomFundId());
    setFieldValue("BLK_FUND_HEADER", "FUND_REF_NO", generateRefNo());
}

// On change of PROFIT_CALC_REQ (checkbox): enable/disable PROFIT_DIST_TYPE
function onChangeProfitCalc() {
    var calc = getFieldValue("BLK_FUND_HEADER", "PROFIT_CALC_REQ");
    if (calc == "Y") {
        enableField("PROFIT_DIST_TYPE");
        showField("FACE_VALUE");
    } else {
        disableField("PROFIT_DIST_TYPE");
        hideField("FACE_VALUE");
    }
}

// Validate Fund ID — must be 6 chars, alphanumeric
function validateFundId() {
    var fid = getFieldValue("BLK_FUND_HEADER", "FUND_ID");
    if (fid == "" || fid == null) {
        return false;
    }
    if (!/^[A-Z0-9]{6}$/.test(fid)) {
        showError("Fund ID must be 6 alphanumeric uppercase characters");
        return false;
    }
    return true;
}

// Validate Face Value — non-negative
function validateFaceValue() {
    var fv = getFieldValue("BLK_FUND_HEADER", "FACE_VALUE");
    if (fv < 0) {
        showError("Face value cannot be negative");
        return false;
    }
    return true;
}

// preSave hook
function preSaveFund() {
    if (!validateFundId()) return false;
    if (!validateFaceValue()) return false;
    return true;
}

// Hookups
fcjFunction.attachOnChange("FUND_PRODUCT",     "onChangeFundProduct");
fcjFunction.attachOnChange("PROFIT_CALC_REQ",  "onChangeProfitCalc");
fcjFunction.attachOnValidate("FUND_ID",        "validateFundId");
fcjFunction.attachOnValidate("FACE_VALUE",     "validateFaceValue");
fcjFunction.preSave("preSaveFund");
