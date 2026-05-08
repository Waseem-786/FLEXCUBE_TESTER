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
**  File Name          : IADASFNL_SYS.js
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
var fieldNameArray = {"BLK_MASTER":"EFFDATE~ASSETCODE~ASSETDESCRIPTION~ASSETCCY~ASSETAMOUNT~SUKKUKHCODE~AVAILABLEAMOUNT~MAKER~MAKERSTAMP~CHECKER~CHECKERSTAMP~MODNO~TXNSTAT~AUTHSTAT~ONCEAUTH","BLK_DETAIL":"EFFDATE~ASSETCODE~SUKKUKHCODE~FUNDID~FUNDDESCRIPTION~ASSETCCY~ALLOCATEDPERCENTAGE~ALLOCATEDAMOUNT","BLK_HIST":"EFFDATE~ASSETCODE~SUKKUKHCODE~FUNDID~FUNDDESCRIPTION~ASSETCCY~ALLOCATEDPERCENTAGE_HIST~ALLOCATEDAMOUNT_HIST"};

var multipleEntryPageSize = {"BLK_DETAIL" :"15" ,"BLK_HIST" :"15" };

var multipleEntrySVBlocks = "";

var tabMEBlks = {"CVS_MAIN__TAB_MAIN":"BLK_DETAIL~BLK_HIST"};

var msgxml=""; 
msgxml += '    <FLD>'; 
msgxml += '      <FN PARENT="" RELATION_TYPE="1" TYPE="BLK_MASTER">EFFDATE~ASSETCODE~ASSETDESCRIPTION~ASSETCCY~ASSETAMOUNT~SUKKUKHCODE~AVAILABLEAMOUNT~MAKER~MAKERSTAMP~CHECKER~CHECKERSTAMP~MODNO~TXNSTAT~AUTHSTAT~ONCEAUTH</FN>'; 
msgxml += '      <FN PARENT="BLK_MASTER" RELATION_TYPE="N" TYPE="BLK_DETAIL">EFFDATE~ASSETCODE~SUKKUKHCODE~FUNDID~FUNDDESCRIPTION~ASSETCCY~ALLOCATEDPERCENTAGE~ALLOCATEDAMOUNT</FN>'; 
msgxml += '      <FN PARENT="BLK_MASTER" RELATION_TYPE="N" TYPE="BLK_HIST">EFFDATE~ASSETCODE~SUKKUKHCODE~FUNDID~FUNDDESCRIPTION~ASSETCCY~ALLOCATEDPERCENTAGE_HIST~ALLOCATEDAMOUNT_HIST</FN>'; 
msgxml += '    </FLD>'; 

var strScreenName = "CVS_MAIN";
var qryReqd = "Y";
var txnBranchFld = "" ;
var originSystem = "";
//----------------------------------------------------------------------------------------------------------------------

//----------------------------------------------------------------------------------------------------------------------
//***** FCJ XML FOR SUMMARY SCREEN *****
//----------------------------------------------------------------------------------------------------------------------
var msgxml_sum=""; 
msgxml_sum += '    <FLD>'; 
msgxml_sum += '      <FN PARENT="" RELATION_TYPE="1" TYPE="BLK_MASTER">AUTHSTAT~TXNSTAT~EFFDATE~ASSETCODE~ASSETDESCRIPTION~ASSETCCY~ASSETAMOUNT~SUKKUKHCODE~AVAILABLEAMOUNT</FN>'; 
msgxml_sum += '    </FLD>'; 

var detailFuncId = "IADASFNL";
var defaultWhereClause = "";
var defaultOrderByClause ="";
var multiBrnWhereClause ="";
var g_SummaryType ="S";
var g_SummaryBtnCount =0;
var g_SummaryBlock ="BLK_MASTER";
//----------------------------------------------------------------------------------------------------------------------
//***** CODE FOR DATABINDING *****
//----------------------------------------------------------------------------------------------------------------------
 var relationArray = {"BLK_MASTER" : "","BLK_DETAIL" : "BLK_MASTER~N","BLK_HIST" : "BLK_MASTER~N"}; 

 var dataSrcLocationArray = new Array("BLK_MASTER","BLK_DETAIL","BLK_HIST"); 
 // Array of all Data Sources used in the screen 
//----------------------------------------------------------------------------------------------------------------------
//***** CODE FOR QUERY MODE *****
//----------------------------------------------------------------------------------------------------------------------
var detailRequired = true ;
var intCurrentQueryResultIndex = 0;
var intCurrentQueryRecordCount = 0;

var queryFields = new Array();    //Values should be set inside IADASFNL.js, in "BlockName__FieldName" format
var pkFields    = new Array();    //Values should be set inside IADASFNL.js, in "BlockName__FieldName" format
queryFields[0] = "BLK_MASTER__EFFDATE";
pkFields[0] = "BLK_MASTER__EFFDATE";
queryFields[1] = "BLK_MASTER__ASSETCODE";
pkFields[1] = "BLK_MASTER__ASSETCODE";
queryFields[2] = "BLK_MASTER__SUKKUKHCODE";
pkFields[2] = "BLK_MASTER__SUKKUKHCODE";
queryFields[3] = "BLK_MASTER__ASSETCCY";
pkFields[3] = "BLK_MASTER__ASSETCCY";
//----------------------------------------------------------------------------------------------------------------------
//***** CODE FOR AMENDABLE/SUBSYSTEM Fields *****
//----------------------------------------------------------------------------------------------------------------------
//***** Fields Amendable while Modification *****
var modifyAmendArr = {"BLK_DETAIL":["ALLOCATEDAMOUNT","ALLOCATEDPERCENTAGE","FUNDDESCRIPTION","FUNDID"],"BLK_HIST":["ALLOCATEDAMOUNT_HIST","ALLOCATEDPERCENTAGE_HIST","FUNDDESCRIPTION","FUNDID"],"BLK_MASTER":["ASSETAMOUNT","AVAILABLEAMOUNT"]};
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
var lovInfoFlds = {"BLK_MASTER__ASSETCODE__LOV_ASSETS":["BLK_MASTER__ASSETCODE~~BLK_MASTER__ASSETDESCRIPTION~BLK_MASTER__ASSETCCY~","","N~N~N~N",""],"BLK_MASTER__SUKKUKHCODE__LOV_SUKKUKH":["BLK_MASTER__SUKKUKHCODE~BLK_MASTER__ASSETCCY~","BLK_MASTER__ASSETCODE!","N~N",""],"BLK_DETAIL__FUNDID__LOV_FUNDID":["BLK_DETAIL__FUNDID~BLK_DETAIL__FUNDDESCRIPTION~","","N~N",""]};
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
var multipleEntryIDs = new Array("BLK_DETAIL","BLK_HIST");
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

ArrFuncOrigin["IADASFNL"]="CUSTOM";
ArrPrntFunc["IADASFNL"]="";
ArrPrntOrigin["IADASFNL"]="";
ArrRoutingType["IADASFNL"]="X";


 // Code for Loading Cluster/Custom js File Starts
ArrClusterModified["IADASFNL"]="N";
ArrCustomModified["IADASFNL"]="Y";

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