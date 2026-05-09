/***************************************************************************************************************************
**  This source is part of the FLEXCUBE Software Product. 
**  Copyright (c) 2008 ,2023, Oracle and/or its affiliates.
**  All rights reserved.
**  
**  No part of this work may be reproduced, stored in a retrieval system, 
**  adopted or transmitted in any form or by any means, electronic, mechanical, photographic, 
**  graphic, optic recording or otherwise, translated in any language or computer language, 
**  without the prior written permission of Oracle and/or its affiliates.
**  
**  Oracle Financial Services Software Limited.
**  Oracle Park, Off Western Express Highway,
**  Goregaon (East),
**  Mumbai - 400 063,
**  India.
**  
**  Written by         : 
**  Date of creation   : 
**  File Name          : IADADHPL_SYS.js
**  Purpose            : 
**  Called From        : 
**  
**  CHANGE LOG
**  Last Modified By   : 
**  Last modified on   : 
**  Full Version       : 
**  Reason             : 
****************************************************************************************************************************/

//***** Code for criteria Search *****
var criteriaSearch  = 'N';
//----------------------------------------------------------------------------------------------------------------------
//***** FCJ XML FOR THE SCREEN *****
//----------------------------------------------------------------------------------------------------------------------
var fieldNameArray = {"BLK_MASTER":"POOL_TYPE~POOL_CURR_OPT~POOL_CCY~POOL_HIBA_PER~POOL_INC~POOL_DIRECT_EXP~POOL_WAKALA_FEE~POOL_WAKALA_INCENT~POOL_DIST_PROFIT~POOL_NET_DIST_PROFIT~POOL_START_DT~POOL_MAT_DT~POOL_DAYS~POOL_ID~POOL_FUND_PROD~POOL_FUND_ID~POOL_MODE~POOL_CUR_WAKAL_FEE~SELECTED_INDEX~POOL_EXTERNAL_REF~RABBUL_MAL_PER~MUDARIB_FEE_PER~SELECTED_INDEX_L~POOL_INC_VW~POOL_DIRECT_EXP_VW~POOL_WAKALA_INCENT_VW~POOL_DIST_PROFIT_VW~POOL_NET_DIST_PROFIT_VW~POOL_CUR_WAKAL_FEE_VW~MAKER~MAKERSTAMP~CHECKER~CHECKERSTAMP~MODNO~TXNSTAT~AUTHSTAT~ONCEAUTH","BLK_ASSET":"POOL_ID~ASSET_CODE~ASSET_DESC~ASSET_CCY~ASSET_ORG_OUT_AMT~ASSET_ORG_OUT_AMT_CONV~ASSET_ALLOC_PER~ASSET_OUT_AMT~ASSET_OUT_AMT_CONV~ASSET_AVG_AMT~ASSET_AVG_AM_CONV~ASSET_PFT_RATE~ASSET_TOT_PFT_AMT~ASSET_TOT_PFT_AMT_CONV~ASSET_TOT_EXP_AMT~ASSET_TOT_EXP_AMT_CONV~EFF_DATE_ST~EFF_DATE_END~ASSET_EXP_RATE~ASSET_AVG_AMT_VW~ASSET_AVG_AMT_CONV_VW~ASSET_TOT_PFT_AMT_VW~ASSET_TOT_PFT_AMT_CONV_VW~ASSET_TOT_EXP_AMT_VW~ASSET_TOT_EXP_AMT_CONV_VW~ASSET_PFT_RATE_VW~ASSET_EXP_RATE_VW","BLK_LIAB":"POOL_ID~LIAB_ID~LIAB_DESC~LIAB_CCY~LIAB_TOT_AMT~LIAB_TOT_AMT_CONV~LIAB_PER_ALLOCATED~LIAB_ALLOC_AMT~LIAB_ALLOC_AMT_CONV~LIAB_PSR~LIAB_YIELD~LIAB_PFT_BFR_HIBA~LIAB_HIBA~LIAB_PFT_AFT_HIBA~LIAB_AVG_AMT~LIAB_AVG_AMT_CONV~LIAB_WGHT~LIAB_WGHT_AMT~LIAB_WGHT_AMT_CONV~LIAB_RATIO~LIAB_YIELD_VW~LIAB_PFT_BFR_HIBA_VW~LIAB_HIBA_VW~LIAB_PFT_AFT_HIBA_VW~LIAB_AVG_AMT_VW~LIAB_AVG_AMT_CONV_VW~LIAB_WGHT_AMT_VW~LIAB_WGHT_AMT_CONV_VW~LIAB_RATIO_VW","BLK_EQUITY":"POOL_ID~EQY_AMOUNT~EQY_PSR~EQY_YIELD~EQY_PFT_BFR_HIBA~EQY_HIBA~EQY_PFT_AFT_HIBA~MUD_FEE_AMT~EQY_PFT_AFT_FEE~EQY_AVG~EQY_YIELD_VW~EQY_PFT_BFR_HIBA_VW~EQY_HIBA_VW~EQY_PFT_AFT_HIBA_VW~EQY_MUD_FEE_AMT_VW~EQY_PFT_AFT_FEE_VW~EQY_AVG_VW~EQY_AMOUNT_VW"};

var multipleEntryPageSize = {"BLK_ASSET" :"15" ,"BLK_LIAB" :"15" };

var multipleEntrySVBlocks = "";

var tabMEBlks = {"CVS_IADADPHL__TAB_MAIN":"BLK_ASSET~BLK_LIAB"};

var msgxml=""; 
msgxml += '    <FLD>'; 
msgxml += '      <FN PARENT="" RELATION_TYPE="1" TYPE="BLK_MASTER">POOL_TYPE~POOL_CURR_OPT~POOL_CCY~POOL_HIBA_PER~POOL_INC~POOL_DIRECT_EXP~POOL_WAKALA_FEE~POOL_WAKALA_INCENT~POOL_DIST_PROFIT~POOL_NET_DIST_PROFIT~POOL_START_DT~POOL_MAT_DT~POOL_DAYS~POOL_ID~POOL_FUND_PROD~POOL_FUND_ID~POOL_MODE~POOL_CUR_WAKAL_FEE~SELECTED_INDEX~POOL_EXTERNAL_REF~RABBUL_MAL_PER~MUDARIB_FEE_PER~SELECTED_INDEX_L~POOL_INC_VW~POOL_DIRECT_EXP_VW~POOL_WAKALA_INCENT_VW~POOL_DIST_PROFIT_VW~POOL_NET_DIST_PROFIT_VW~POOL_CUR_WAKAL_FEE_VW~MAKER~MAKERSTAMP~CHECKER~CHECKERSTAMP~MODNO~TXNSTAT~AUTHSTAT~ONCEAUTH</FN>'; 
msgxml += '      <FN PARENT="BLK_MASTER" RELATION_TYPE="N" TYPE="BLK_ASSET">POOL_ID~ASSET_CODE~ASSET_DESC~ASSET_CCY~ASSET_ORG_OUT_AMT~ASSET_ORG_OUT_AMT_CONV~ASSET_ALLOC_PER~ASSET_OUT_AMT~ASSET_OUT_AMT_CONV~ASSET_AVG_AMT~ASSET_AVG_AM_CONV~ASSET_PFT_RATE~ASSET_TOT_PFT_AMT~ASSET_TOT_PFT_AMT_CONV~ASSET_TOT_EXP_AMT~ASSET_TOT_EXP_AMT_CONV~EFF_DATE_ST~EFF_DATE_END~ASSET_EXP_RATE~ASSET_AVG_AMT_VW~ASSET_AVG_AMT_CONV_VW~ASSET_TOT_PFT_AMT_VW~ASSET_TOT_PFT_AMT_CONV_VW~ASSET_TOT_EXP_AMT_VW~ASSET_TOT_EXP_AMT_CONV_VW~ASSET_PFT_RATE_VW~ASSET_EXP_RATE_VW</FN>'; 
msgxml += '      <FN PARENT="BLK_MASTER" RELATION_TYPE="N" TYPE="BLK_LIAB">POOL_ID~LIAB_ID~LIAB_DESC~LIAB_CCY~LIAB_TOT_AMT~LIAB_TOT_AMT_CONV~LIAB_PER_ALLOCATED~LIAB_ALLOC_AMT~LIAB_ALLOC_AMT_CONV~LIAB_PSR~LIAB_YIELD~LIAB_PFT_BFR_HIBA~LIAB_HIBA~LIAB_PFT_AFT_HIBA~LIAB_AVG_AMT~LIAB_AVG_AMT_CONV~LIAB_WGHT~LIAB_WGHT_AMT~LIAB_WGHT_AMT_CONV~LIAB_RATIO~LIAB_YIELD_VW~LIAB_PFT_BFR_HIBA_VW~LIAB_HIBA_VW~LIAB_PFT_AFT_HIBA_VW~LIAB_AVG_AMT_VW~LIAB_AVG_AMT_CONV_VW~LIAB_WGHT_AMT_VW~LIAB_WGHT_AMT_CONV_VW~LIAB_RATIO_VW</FN>'; 
msgxml += '      <FN PARENT="BLK_MASTER" RELATION_TYPE="1" TYPE="BLK_EQUITY">POOL_ID~EQY_AMOUNT~EQY_PSR~EQY_YIELD~EQY_PFT_BFR_HIBA~EQY_HIBA~EQY_PFT_AFT_HIBA~MUD_FEE_AMT~EQY_PFT_AFT_FEE~EQY_AVG~EQY_YIELD_VW~EQY_PFT_BFR_HIBA_VW~EQY_HIBA_VW~EQY_PFT_AFT_HIBA_VW~EQY_MUD_FEE_AMT_VW~EQY_PFT_AFT_FEE_VW~EQY_AVG_VW~EQY_AMOUNT_VW</FN>'; 
msgxml += '    </FLD>'; 

var strScreenName = "CVS_IADADPHL";
var qryReqd = "Y";
var txnBranchFld = "" ;
var originSystem = "";
//----------------------------------------------------------------------------------------------------------------------

//----------------------------------------------------------------------------------------------------------------------
//***** FCJ XML FOR SUMMARY SCREEN *****
//----------------------------------------------------------------------------------------------------------------------
var msgxml_sum=""; 
msgxml_sum += '    <FLD>'; 
msgxml_sum += '      <FN PARENT="" RELATION_TYPE="1" TYPE="BLK_MASTER">AUTHSTAT~TXNSTAT~POOL_FUND_ID~POOL_FUND_PROD~POOL_ID~POOL_MODE~POOL_TYPE~POOL_CURR_OPT~POOL_CCY</FN>'; 
msgxml_sum += '    </FLD>'; 

var detailFuncId = "IADADHPL";
var defaultWhereClause = "";
var defaultOrderByClause ="";
var multiBrnWhereClause ="";
var g_SummaryType ="S";
var g_SummaryBtnCount =0;
var g_SummaryBlock ="BLK_MASTER";
//----------------------------------------------------------------------------------------------------------------------
//***** CODE FOR DATABINDING *****
//----------------------------------------------------------------------------------------------------------------------
 var relationArray = {"BLK_MASTER" : "","BLK_ASSET" : "BLK_MASTER~N","BLK_LIAB" : "BLK_MASTER~N","BLK_EQUITY" : "BLK_MASTER~1"}; 

 var dataSrcLocationArray = new Array("BLK_MASTER","BLK_ASSET","BLK_LIAB","BLK_EQUITY"); 
 // Array of all Data Sources used in the screen 
//----------------------------------------------------------------------------------------------------------------------
//***** CODE FOR QUERY MODE *****
//----------------------------------------------------------------------------------------------------------------------
var detailRequired = true ;
var intCurrentQueryResultIndex = 0;
var intCurrentQueryRecordCount = 0;

var queryFields = new Array();    //Values should be set inside IADADHPL.js, in "BlockName__FieldName" format
var pkFields    = new Array();    //Values should be set inside IADADHPL.js, in "BlockName__FieldName" format
queryFields[0] = "BLK_MASTER__POOL_ID";
pkFields[0] = "BLK_MASTER__POOL_ID";
//----------------------------------------------------------------------------------------------------------------------
//***** CODE FOR AMENDABLE/SUBSYSTEM Fields *****
//----------------------------------------------------------------------------------------------------------------------
//***** Fields Amendable while Modification *****
var modifyAmendArr = {"BLK_ASSET":["ASSET_ALLOC_PER","ASSET_AVG_AMT","ASSET_AVG_AMT_CONV_VW","ASSET_AVG_AMT_VW","ASSET_AVG_AM_CONV","ASSET_CCY","ASSET_CODE","ASSET_DESC","ASSET_EXP_RATE","ASSET_EXP_RATE_VW","ASSET_ORG_OUT_AMT","ASSET_ORG_OUT_AMT_CONV","ASSET_OUT_AMT","ASSET_OUT_AMT_CONV","ASSET_PFT_RATE","ASSET_PFT_RATE_VW","ASSET_TOT_EXP_AMT","ASSET_TOT_EXP_AMT_CONV","ASSET_TOT_EXP_AMT_CONV_VW","ASSET_TOT_EXP_AMT_VW","ASSET_TOT_PFT_AMT","ASSET_TOT_PFT_AMT_CONV","ASSET_TOT_PFT_AMT_CONV_VW","ASSET_TOT_PFT_AMT_VW","EFF_DATE_ENDI","EFF_DATE_STI"],"BLK_EQUITY":["EQY_AMOUNT","EQY_AMOUNT_VW","EQY_AVG","EQY_AVG_VW","EQY_HIBA","EQY_HIBA_VW","EQY_MUD_FEE_AMT_VW","EQY_PFT_AFT_FEE","EQY_PFT_AFT_FEE_VW","EQY_PFT_AFT_HIBA","EQY_PFT_AFT_HIBA_VW","EQY_PFT_BFR_HIBA","EQY_PFT_BFR_HIBA_VW","EQY_PSR","EQY_YIELD","EQY_YIELD_VW","MUD_FEE_AMT"],"BLK_LIAB":["LIAB_ALLOC_AMT","LIAB_ALLOC_AMT_CONV","LIAB_AVG_AMT","LIAB_AVG_AMT_CONV","LIAB_AVG_AMT_CONV_VW","LIAB_AVG_AMT_VW","LIAB_CCY","LIAB_DESC","LIAB_HIBA","LIAB_HIBA_VW","LIAB_ID","LIAB_PER_ALLOCATED","LIAB_PFT_AFT_HIBA","LIAB_PFT_AFT_HIBA_VW","LIAB_PFT_BFR_HIBA","LIAB_PFT_BFR_HIBA_VW","LIAB_PSR","LIAB_RATIO","LIAB_RATIO_VW","LIAB_TOT_AMT","LIAB_TOT_AMT_CONV","LIAB_WGHT","LIAB_WGHT_AMT","LIAB_WGHT_AMT_CONV","LIAB_WGHT_AMT_CONV_VW","LIAB_WGHT_AMT_VW","LIAB_YIELD","LIAB_YIELD_VW"],"BLK_MASTER":["MUDARIB_FEE_PER","POOL_CUR_WAKAL_FEE","POOL_CUR_WAKAL_FEE_VW","POOL_DAYS","POOL_DIRECT_EXP","POOL_DIRECT_EXP_VW","POOL_DIST_PROFIT","POOL_DIST_PROFIT_VW","POOL_HIBA_PER","POOL_INC","POOL_INC_VW","POOL_MAT_DTI","POOL_MODE","POOL_NET_DIST_PROFIT","POOL_NET_DIST_PROFIT_VW","POOL_WAKALA_FEE","POOL_WAKALA_INCENT","POOL_WAKALA_INCENT_VW","RABBUL_MAL_PER","SELECTED_INDEX","SELECTED_INDEX_L"]};
var closeAmendArr = new Array(); 
var reopenAmendArr = new Array(); 
var reverseAmendArr = new Array(); 
var deleteAmendArr = new Array(); 
var rolloverAmendArr = new Array(); 
var confirmAmendArr = new Array(); 
var liquidateAmendArr = new Array(); 
var queryAmendArr = new Array(); 
var authorizeAmendArr = new Array(); 
//----------------------------------------------------------------------------------------------------------------------

var subsysArr    = new Array(); 

//----------------------------------------------------------------------------------------------------------------------

//***** CODE FOR LOVs *****
//----------------------------------------------------------------------------------------------------------------------
var lovInfoFlds = {"BLK_MASTER__POOL_CCY__LOV_CCY":["BLK_MASTER__POOL_CCY~~","BLK_MASTER__POOL_CURR_OPT!VARCHAR2~BLK_MASTER__POOL_CURR_OPT!VARCHAR2","N~N",""],"BLK_MASTER__POOL_FUND_PROD__LOV_FUND_PRODUCT":["BLK_MASTER__POOL_FUND_PROD~~","","N~N",""],"BLK_MASTER__POOL_FUND_ID__LOV_FUND_ID":["BLK_MASTER__POOL_FUND_ID~~","BLK_MASTER__POOL_FUND_PROD!VARCHAR2","N~N",""],"BLK_ASSET__ASSET_CODE__LOV_ASSET":["BLK_ASSET__ASSET_CODE~BLK_ASSET__ASSET_DESC~BLK_ASSET__ASSET_CCY~BLK_ASSET__ASSET_ORG_OUT_AMT~","","N~N~N~N",""],"BLK_LIAB__LIAB_ID__LOV_BORROWER":["BLK_LIAB__LIAB_ID~BLK_LIAB__LIAB_DESC~BLK_LIAB__LIAB_CCY~BLK_LIAB__LIAB_TOT_AMT~","BLK_MASTER__POOL_MAT_DT!DATE~BLK_MASTER__POOL_START_DT!DATE~BLK_MASTER__POOL_MAT_DT!DATE~BLK_MASTER__POOL_START_DT!DATE~BLK_MASTER__POOL_TYPE!VARCHAR2~BLK_MASTER__POOL_TYPE!VARCHAR2~BLK_MASTER__POOL_FUND_ID!VARCHAR2~BLK_MASTER__POOL_START_DT!DATE~BLK_MASTER__POOL_MAT_DT!DATE~BLK_MASTER__POOL_TYPE!VARCHAR2~BLK_MASTER__POOL_TYPE!VARCHAR2","N~N~N~N",""]};
var offlineLovInfoFlds = {};
//----------------------------------------------------------------------------------------------------------------------
//***** SCRIPT FOR TABS *****
//----------------------------------------------------------------------------------------------------------------------
var strHeaderTabId = 'TAB_HEADER';
var strFooterTabId = 'TAB_FOOTER';
var strCurrentTabId = 'TAB_MAIN';
//--------------------------------------------
//----------------------------------------------------------------------------------------------------------------------
//***** SCRIPT FOR MULTIPLE ENTRY BLOCKS *****
//----------------------------------------------------------------------------------------------------------------------
var multipleEntryIDs = new Array("BLK_ASSET","BLK_LIAB");
var multipleEntryArray = new Array();
var multipleEntryCells = new Array();
//----------------------------------------------------------------------------------------------------------------------
//***** SCRIPT FOR MULTIPLE ENTRY VIEW SINGLE ENTRY BLOCKS *****
//----------------------------------------------------------------------------------------------------------------------

//----------------------------------------------------------------------------------------------------------------------
//***** SCRIPT FOR ATTACHED CALLFORMS *****
 //----------------------------------------------------------------------------------------------------------------------

 var CallFormArray= new Array(); 

 var CallFormRelat=new Array(); 

 var CallRelatType= new Array(); 


 var ArrFuncOrigin=new Array();
 var ArrPrntFunc=new Array();
 var ArrPrntOrigin=new Array();
 var ArrRoutingType=new Array();


 // Code for Loading Cluster/Custom js File Starts
 var ArrClusterModified=new Array();
 var ArrCustomModified=new Array();
 // Code for Loading Cluster/Custom js File ends

ArrFuncOrigin["IADADHPL"]="CUSTOM";
ArrPrntFunc["IADADHPL"]="";
ArrPrntOrigin["IADADHPL"]="";
ArrRoutingType["IADADHPL"]="X";


 // Code for Loading Cluster/Custom js File Starts
ArrClusterModified["IADADHPL"]="N";
ArrCustomModified["IADADHPL"]="Y";

 // Code for Loading Cluster/Custom js File ends


 /* Code For OBIEE functionalities */ 
var obScrArgName  = new Array(); 
var obScrArgSource  = new Array(); 
//***** CODE FOR SCREEN ARGS *****
//----------------------------------------------------------------------------------------------------------------------
var scrArgName = {};
var scrArgSource = {};
var scrArgVals = {};
var scrArgDest = {};
//***** CODE FOR SUB-SYSTEM DEPENDENT  FIELDS   *****
//----------------------------------------------------------------------------------------------------------------------
var dpndntOnFlds = {};
var dpndntOnSrvs = {};
//***** CODE FOR TAB DEPENDENT  FIELDS   *****
//----------------------------------------------------------------------------------------------------------------------
//***** CODE FOR CALLFORM TABS *****
//----------------------------------------------------------------------------------------------------------------------
var callformTabArray = new Array(); 
//***** CODE FOR ACTION STAGE DETAILS *****
//----------------------------------------------------------------------------------------------------------------------
var actStageArry = {};
//***** CODE FOR IMAGE FLDSET *****
//----------------------------------------------------------------------------------------------------------------------